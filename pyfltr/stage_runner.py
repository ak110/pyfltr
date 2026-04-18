"""cli.py / ui.py が共用するステージ実行ヘルパー。"""

import concurrent.futures

import pyfltr.command
import pyfltr.config


def make_skipped_result(command: str, config: pyfltr.config.Config) -> pyfltr.command.CommandResult:
    """--fail-fast 中断対象の skipped CommandResult を作る。"""
    command_info = config.commands[command]
    return pyfltr.command.CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=[],
        returncode=None,
        has_error=False,
        files=0,
        output="--fail-fast により実行をスキップしました。",
        elapsed=0.0,
    )


def cancel_pending_futures(
    future_to_command: dict[concurrent.futures.Future, str],
    aborted_commands: set[str],
) -> None:
    """未開始ジョブをキャンセルし、中断対象コマンド名を aborted_commands に追加する。

    done() の future は対象外とする。cancel() が True を返した（キャンセル成功）
    ものだけを aborted_commands に登録する。
    """
    for future, command in future_to_command.items():
        if future.done():
            continue
        if future.cancel():
            aborted_commands.add(command)
