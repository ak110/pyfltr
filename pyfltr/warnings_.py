"""構造化された警告の収集。

`logger.warning` による stderr 出力に加えて、警告内容を内部リストへ蓄積し、
`--output-format=jsonl` / text / TUI の各レンダラが終盤でまとめて表示できるようにする。
"""

import logging
import traceback
import typing

logger = logging.getLogger(__name__)

_warnings: list[dict[str, typing.Any]] = []


def emit_warning(source: str, message: str, *, exc_info: bool = False, hint: str | None = None) -> None:
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
    _warnings.append(entry)


def collected_warnings() -> list[dict[str, typing.Any]]:
    """蓄積された警告の浅いコピーを返す。"""
    return list(_warnings)


def clear() -> None:
    """蓄積を初期化する。"""
    _warnings.clear()
