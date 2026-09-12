"""構造化された警告の収集。

`logger.warning`によるstderr出力に加えて、警告内容を内部リストへ蓄積し、
`--output-format=jsonl` / text / TUIの各レンダラが終盤でまとめて表示できるようにする。
"""

import collections.abc
import contextlib
import contextvars
import dataclasses
import logging
import threading
import traceback
import typing

logger = logging.getLogger(__name__)

FilteredReason = typing.Literal["excluded", "missing", "external"]

_DuplicateSuppressionState = tuple[
    frozenset[tuple[str, str]],
    frozenset[tuple[str, FilteredReason]],
]

_warnings: list[dict[str, typing.Any]] = []
_filtered_direct_files: list[tuple[str, FilteredReason]] = []
_delivery_lock = threading.RLock()


@dataclasses.dataclass
class _DeliveryState:
    """stderrへの警告配送を保留するスコープの共有状態。"""

    depth: int = 0
    deferred: list[dict[str, typing.Any]] = dataclasses.field(default_factory=list)
    delivered_ids: set[int] = dataclasses.field(default_factory=set)


_delivery_state = _DeliveryState()
_duplicate_suppression_state: contextvars.ContextVar[_DuplicateSuppressionState | None] = contextvars.ContextVar(
    "duplicate_suppression_state",
    default=None,
)


@contextlib.contextmanager
def suppress_duplicates() -> collections.abc.Iterator[None]:
    """スコープ内で同一の警告と除外記録の2回目以降を抑止する。

    既に外側のスコープが有効な場合は状態を再初期化せず、外側の既出組をそのまま共有する
    （ネスト時に内側スコープが外側の既出組を空へ戻す事故を防ぐため）。
    """
    if _duplicate_suppression_state.get() is not None:
        yield
        return
    token = _duplicate_suppression_state.set((frozenset(), frozenset()))
    try:
        yield
    finally:
        _duplicate_suppression_state.reset(token)


@contextlib.contextmanager
def defer_stderr() -> collections.abc.Iterator[None]:
    """JSONLへの配送結果が確定するまで警告のstderr通知を保留する。

    正常にJSONLへ含めた警告は`mark_delivered`で通知済みにする。
    例外や早期終了により配送されなかった警告は、最外スコープの終了時にstderrへ1回だけ通知する。
    """
    with _delivery_lock:
        _delivery_state.depth += 1
    try:
        yield
    finally:
        pending: list[dict[str, typing.Any]] = []
        with _delivery_lock:
            _delivery_state.depth -= 1
            if _delivery_state.depth == 0:
                pending = [entry for entry in _delivery_state.deferred if id(entry) not in _delivery_state.delivered_ids]
                _delivery_state.deferred.clear()
                _delivery_state.delivered_ids.clear()
        for entry in pending:
            logger.warning(entry["message"])


def mark_delivered(entries: collections.abc.Iterable[dict[str, typing.Any]]) -> None:
    """消費主体へ正常に配送した警告をstderr通知済みとして記録する。"""
    with _delivery_lock:
        if _delivery_state.depth == 0:
            return
        _delivery_state.delivered_ids.update(id(entry) for entry in entries)


def emit_warning(source: str, message: str, *, exc_info: bool = False, hint: str | None = None) -> None:
    """警告を蓄積し、配送保留スコープ外ではstderrへ即時通知する。

    `exc_info=True`を指定すると`traceback.format_exc()`の内容を`message`末尾に
    連結して蓄積する（JSONLなどloggerを通さない経路でもスタックトレースを参照できるように）。

    `hint`は当該警告に固有の対処手順（例: 「識別子をバックティックで囲む」）を
    短く示す文字列。指定時のみ蓄積dictに`hint`キーとして含める。
    `summary.guidance`は失敗時の包括的な案内を担うのに対し、本フィールドは
    個別warning単位のヒントとして分離する。
    """
    suppression_state = _duplicate_suppression_state.get()
    if suppression_state is not None:
        warning_keys, _ = suppression_state
        key = (source, message)
        if key in warning_keys:
            return
        _duplicate_suppression_state.set((warning_keys | {key}, suppression_state[1]))
    stored = message
    if exc_info:
        tb = traceback.format_exc().rstrip()
        if tb and tb != "NoneType: None":
            stored = f"{message}\n{tb}"
    entry: dict[str, typing.Any] = {"source": source, "message": stored}
    if hint is not None:
        entry["hint"] = hint
    with _delivery_lock:
        _warnings.append(entry)
        if _delivery_state.depth > 0:
            _delivery_state.deferred.append(entry)
            return
    logger.warning(message, exc_info=exc_info)


def collected_warnings() -> list[dict[str, typing.Any]]:
    """蓄積された警告の浅いコピーを返す。"""
    return list(_warnings)


def add_filtered_direct_file(path: str, *, reason: FilteredReason) -> None:
    """直接指定されたが対象から外れたファイルをreason付きで蓄積する。

    `reason="excluded"`はexclude/.gitignore設定で除外されたケース、
    `reason="missing"`は指定パスが存在しないケースを表し、summaryへ
    `fully_excluded_files`・`missing_targets`として明示することで、
    「警告0件 + exit 0」を「問題なし」と誤解しないようにする。
    `reason="external"`は`allows_external_paths=False`のツールに対して
    起点cwd配下にない絶対パスが指定されたケースを表す（ツール別の除外）。
    `external`は`emit_warning`経由のwarning出力のみで利用者へ伝え、
    summaryフィールドへの集計は行わない（パイプライン全体ではなく
    特定ツールに対する個別除外であり、`fully_excluded_files`が想定する
    「全ツール共通の除外」と性質が異なるため）。
    警告ログ出力は呼び出し側で`emit_warning`が既に担うため、本関数では蓄積のみ行う。
    """
    suppression_state = _duplicate_suppression_state.get()
    if suppression_state is not None:
        _, filtered_file_keys = suppression_state
        key = (path, reason)
        if key in filtered_file_keys:
            return
        _duplicate_suppression_state.set((suppression_state[0], filtered_file_keys | {key}))
    _filtered_direct_files.append((path, reason))


def filtered_direct_files(*, reason: FilteredReason | None = None) -> list[str]:
    """蓄積された直接指定フィルタ対象ファイル一覧の浅いコピーを返す。

    `reason`を指定すると当該理由のものだけに限定する。
    未指定時は理由を問わず全件を順序通りに返す。
    """
    if reason is None:
        return [path for path, _ in _filtered_direct_files]
    return [path for path, r in _filtered_direct_files if r == reason]


def clear() -> None:
    """蓄積を初期化する。"""
    _warnings.clear()
    _filtered_direct_files.clear()
