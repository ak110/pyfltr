"""Claude Code hook用のdirty fileトラッキング。"""

# dirty → main の遅延importによる循環参照は実行時に問題ないため抑制
# pylint: disable=cyclic-import

import json
import logging
import pathlib
import sys

logger = logging.getLogger(__name__)


def run_dirty(args: list[str], *, base_dir: pathlib.Path | None = None) -> int:
    """dirtyサブコマンドのエントリポイント。

    常にexit 0を返す（フック失敗でセッションを止めない）。
    """
    try:
        if not args:
            logger.error("dirtyサブコマンドにはinit/add/runのいずれかを指定してください。")
            return 0

        dirty_file = (base_dir or pathlib.Path.cwd()) / ".claude" / ".format-dirty"
        sub = args[0]
        if sub == "init":
            _init(dirty_file)
        elif sub == "add":
            _add(dirty_file)
        elif sub == "run":
            _run(dirty_file)
        else:
            logger.error(f"不明なdirtyサブコマンド: {sub}")
    except Exception:
        logger.debug("dirtyサブコマンドで例外が発生しました。", exc_info=True)
    return 0


def _init(dirty_file: pathlib.Path) -> None:
    """.claude/.format-dirtyを削除する。"""
    dirty_file.unlink(missing_ok=True)
    logger.debug("dirty fileを初期化しました。")


def _add(dirty_file: pathlib.Path) -> None:
    """stdinからClaude Code hook JSONを読み、編集ファイルを.format-dirtyに追記する。"""
    data = json.load(sys.stdin)
    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        return

    # 重複チェック
    existing: set[str] = set()
    if dirty_file.exists():
        existing = set(dirty_file.read_text(encoding="utf-8").splitlines())

    if file_path in existing:
        return

    dirty_file.parent.mkdir(parents=True, exist_ok=True)
    with dirty_file.open("a", encoding="utf-8") as f:
        f.write(file_path + "\n")
    logger.debug(f"dirty fileに追加: {file_path}")


def _run(dirty_file: pathlib.Path) -> None:
    """.format-dirtyのファイルリストをfast相当で整形し、.format-dirtyを削除する。"""
    if not dirty_file.exists():
        logger.debug("dirty fileが存在しないためスキップします。")
        return

    lines = dirty_file.read_text(encoding="utf-8").splitlines()
    # 存在するファイルのみフィルタ
    files = [f for f in lines if f and pathlib.Path(f).exists()]

    if files:
        from pyfltr.main import run as pyfltr_run  # pylint: disable=import-outside-toplevel

        pyfltr_run(["fast", "--no-clear", "--no-ui", *files])

    dirty_file.unlink(missing_ok=True)
    logger.debug("dirty fileを整形して削除しました。")
