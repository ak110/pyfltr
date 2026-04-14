"""構造化された警告の収集。

`logger.warning` による stderr 出力に加えて、警告内容を内部リストへ蓄積し、
`--output-format=jsonl` / text / TUI の各レンダラが終盤でまとめて表示できるようにする。
"""

import logging
import traceback
import typing

logger = logging.getLogger(__name__)

_warnings: list[dict[str, typing.Any]] = []


def emit_warning(source: str, message: str, *, exc_info: bool = False) -> None:
    """警告を発行し、ログ出力と内部蓄積を同時に行う。

    ``exc_info=True`` を指定すると ``traceback.format_exc()`` の内容を ``message`` 末尾に
    連結して蓄積する（JSONL など logger を通さない経路でもスタックトレースを参照できるように）。
    """
    logger.warning(message, exc_info=exc_info)
    stored = message
    if exc_info:
        tb = traceback.format_exc().rstrip()
        if tb and tb != "NoneType: None":
            stored = f"{message}\n{tb}"
    _warnings.append({"source": source, "message": stored})


def collected_warnings() -> list[dict[str, typing.Any]]:
    """蓄積された警告の浅いコピーを返す。"""
    return list(_warnings)


def clear() -> None:
    """蓄積を初期化する。"""
    _warnings.clear()
