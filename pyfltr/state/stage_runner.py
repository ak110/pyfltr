"""cli/pipeline.py / output/ui.py が共用するステージ実行ヘルパー。"""

import concurrent.futures

import pyfltr.command.core_
import pyfltr.config.config


def make_skipped_result(
    command: str,
    config: pyfltr.config.config.Config,
    *,
    reason: str | None = None,
) -> pyfltr.command.core_.CommandResult:
    """中断対象の skipped CommandResult を生成する。

    `reason`が指定された場合は`CommandResult.output`に反映する。省略時は既定の
    `--fail-fast`文言（従来互換）を使う。TUIのCtrl+C協調停止経路では固有の文言を
    渡して出力する。
    """
    command_info = config.commands[command]
    output = reason if reason is not None else "--fail-fast により実行をスキップしました。"
    return pyfltr.command.core_.CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=[],
        returncode=None,
        has_error=False,
        files=0,
        output=output,
        elapsed=0.0,
    )


def cancel_pending_futures(
    future_to_command: dict[concurrent.futures.Future, str],
    aborted_commands: set[str],
) -> None:
    """未開始ジョブをキャンセルし、中断対象コマンド名を`aborted_commands`に追加する。

    `done()`のfutureは対象外とする。`cancel()`がTrueを返した（キャンセル成功）
    ものだけを`aborted_commands`に登録する。
    """
    for future, command in future_to_command.items():
        if future.done():
            continue
        if future.cancel():
            aborted_commands.add(command)
