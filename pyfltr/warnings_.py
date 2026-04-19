"""構造化された警告の収集。

`logger.warning` による stderr 出力に加えて、警告内容を内部リストへ蓄積し、
`--output-format=jsonl` / text / TUI の各レンダラが終盤でまとめて表示できるようにする。
"""

import logging
import traceback
import typing

logger = logging.getLogger(__name__)


class WarningCollector:
    """警告エントリのコレクター。

    グローバル変数による直接管理を本クラスに集約し、テストから差し替え可能な構造にする。
    既存コードはスレッドセーフでないため、ロックは導入しない（既存挙動を踏襲）。
    """

    def __init__(self) -> None:
        self._warnings: list[dict[str, typing.Any]] = []

    def emit(
        self,
        *,
        source: str,
        message: str,
        exc_info: bool = False,
        hint: str | None = None,
    ) -> None:
        """警告を発行し、ログ出力と内部蓄積を同時に行う。

        ``exc_info=True`` を指定すると ``traceback.format_exc()`` の内容を ``message`` 末尾に
        連結して蓄積する（JSONL など logger を通さない経路でもスタックトレースを参照できるように）。

        ``hint`` は当該警告に固有の対処手順（例: 「識別子をバックティックで囲む」）を
        短く示す文字列。指定時のみ蓄積 dict に ``hint`` キーとして含める。
        ``summary.guidance`` は失敗時の包括的な案内を担うのに対し、本フィールドは
        個別 warning 単位のヒントとして分離する。
        """
        logger.warning(message, exc_info=exc_info)
        stored = message
        if exc_info:
            tb = traceback.format_exc().rstrip()
            if tb and tb != "NoneType: None":
                stored = f"{message}\n{tb}"
        entry: dict[str, typing.Any] = {"source": source, "message": stored}
        if hint is not None:
            entry["hint"] = hint
        self._warnings.append(entry)

    def collected(self) -> list[dict[str, typing.Any]]:
        """蓄積された警告の浅いコピーを返す。"""
        return list(self._warnings)

    def clear(self) -> None:
        """蓄積を初期化する。"""
        self._warnings.clear()


_DEFAULT_COLLECTOR = WarningCollector()


def set_default_collector(collector: WarningCollector) -> None:
    """デフォルトの WarningCollector を差し替える（テスト用経路）。

    本 Phase では既存テストを書き換えないが、今後のテストが独自インスタンスを使いたい
    場合のために用意する。
    """
    global _DEFAULT_COLLECTOR  # pylint: disable=global-statement
    _DEFAULT_COLLECTOR = collector


def emit_warning(source: str, message: str, *, exc_info: bool = False, hint: str | None = None) -> None:
    """警告を発行し、ログ出力と内部蓄積を同時に行う（ファサード）。

    ``_DEFAULT_COLLECTOR.emit()`` に委譲する。
    """
    _DEFAULT_COLLECTOR.emit(source=source, message=message, exc_info=exc_info, hint=hint)


def collected_warnings() -> list[dict[str, typing.Any]]:
    """蓄積された警告の浅いコピーを返す（ファサード）。

    ``_DEFAULT_COLLECTOR.collected()`` に委譲する。
    """
    return _DEFAULT_COLLECTOR.collected()


def clear() -> None:
    """蓄積を初期化する（ファサード）。

    ``_DEFAULT_COLLECTOR.clear()`` に委譲する。
    """
    _DEFAULT_COLLECTOR.clear()
