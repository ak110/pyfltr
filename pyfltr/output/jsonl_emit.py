"""JSONLの排他書込みと最終出力時刻。"""

import threading
import time
import typing

import pyfltr.output.logging_

# 複数行を1つの排他境界で書き込み、heartbeatの最終出力時刻と同期させる。
_write_lock = threading.Lock()


class _JsonlOutputTimeState:
    """最終JSONL出力時刻の可変状態保持クラス。

    可変状態をインスタンスへ局所化することでモジュール変数とglobal文を排除し、
    テストでのmonkeypatchによる差し替えも容易にする。
    初期値 None は未出力（プロセス起動直後）を表す。
    """

    monotonic_time: float | None = None


_output_time_state = _JsonlOutputTimeState()


def get_last_jsonl_output_time() -> float | None:
    """`last_jsonl_output_time` を返す（heartbeat監視スレッドからの参照用）。"""
    with _write_lock:
        return _output_time_state.monotonic_time


def set_last_jsonl_output_time(value: float) -> None:
    """`last_jsonl_output_time` を更新する（heartbeat発火後の発火時刻記録用）。"""
    with _write_lock:
        _output_time_state.monotonic_time = value


def _emit_structured(line: str) -> None:
    """構造化出力を書き込みつつ、最終出力時刻を更新する。

    呼び出し元はあらかじめ `_write_lock` を取得していること（複数行のアトミック書き込みを担保するため）。
    """
    pyfltr.output.logging_.structured_logger.info(line)
    _output_time_state.monotonic_time = time.monotonic()


def emit_record(line: str) -> None:
    """構造化出力を1行書き込む公開ヘルパー（モジュール外向けの正規API）。

    `_write_lock` / `_emit_structured` は内部実装としてモジュール外には公開しない。
    `_emit_structured` が最終出力時刻 (`_output_time_state.monotonic_time`) を更新するため、
    新規イベント種別を追加する場合も本ヘルパー経由を維持し、heartbeat監視スレッドが
    「最後のJSONL出力からの無音時間」を判定できる状態を崩さない。
    `pylint: disable=protected-access` の常態化を避ける目的でも公開ヘルパーへ集約する。
    順序保証が不要な単発書き込み（grep/replace等のレコード）に向く。
    複数行をアトミックに出力する場合は `emit_records` を使う。
    """
    with _write_lock:
        _emit_structured(line)


def emit_records(lines: typing.Iterable[str]) -> None:
    """複数行を1ロック内でアトミックに書き込む公開ヘルパー（モジュール外向けの正規API）。

    並列実行されるツールから同時にコールバックされても、グルーピング単位
    （diagnostic + commandの組み合わせ等）が崩れないようロック保護する。
    各行は `_emit_structured` 経由で最終出力時刻も更新するため、heartbeat連動が保たれる。
    `_write_lock` / `_emit_structured` への直接アクセスは禁止し、本ヘルパーを正規APIとする。
    """
    with _write_lock:
        for line in lines:
            _emit_structured(line)
