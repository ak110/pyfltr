"""Claude Code hook用のdirty fileトラッキング。"""

# dirty → main の遅延importによる循環参照は実行時に問題ないため抑制
# pylint: disable=cyclic-import

import json
import logging
import pathlib
import sys

logger = logging.getLogger(__name__)

_DIRTY_FILE = pathlib.Path(".claude/.format-dirty")


def run_dirty(args: list[str]) -> int:
    """dirtyサブコマンドのエントリポイント。

    常にexit 0を返す（フック失敗でセッションを止めない）。
    """
    try:
        if not args:
            logger.error("dirtyサブコマンドにはinit/add/runのいずれかを指定してください。")
            return 0

        sub = args[0]
        if sub == "init":
            _init()
        elif sub == "add":
            _add()
        elif sub == "run":
            _run()
        else:
            logger.error(f"不明なdirtyサブコマンド: {sub}")
    except Exception:
        logger.debug("dirtyサブコマンドで例外が発生しました。", exc_info=True)
    return 0


def _init() -> None:
    """.claude/.format-dirtyを削除する。"""
    _DIRTY_FILE.unlink(missing_ok=True)
    logger.debug("dirty file を初期化しました。")


def _add() -> None:
    """stdinからClaude Code hook JSONを読み、編集ファイルを.format-dirtyに追記する。"""
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    # 重複チェック
    existing: set[str] = set()
    if _DIRTY_FILE.exists():
        existing = set(_DIRTY_FILE.read_text(encoding="utf-8").splitlines())

    if file_path in existing:
        return

    _DIRTY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _DIRTY_FILE.open("a", encoding="utf-8") as f:
        f.write(file_path + "\n")
    logger.debug(f"dirty fileに追加: {file_path}")


def _run() -> None:
    """.format-dirtyのファイルリストをfast相当で整形し、.format-dirtyを削除する。"""
    if not _DIRTY_FILE.exists():
        logger.debug("dirty fileが存在しないためスキップします。")
        return

    lines = _DIRTY_FILE.read_text(encoding="utf-8").splitlines()
    # 存在するファイルのみフィルタ
    files = [f for f in lines if f and pathlib.Path(f).exists()]

    if files:
        from pyfltr.main import run as pyfltr_run  # pylint: disable=import-outside-toplevel

        pyfltr_run(["fast", "--no-clear", "--no-ui", *files])

    _DIRTY_FILE.unlink(missing_ok=True)
    logger.debug("dirty fileを整形して削除しました。")
