"""cli/pipeline.py / output/ui.py が共用するステージ実行ヘルパー。"""

import collections.abc
import concurrent.futures
import dataclasses

import pyfltr.command.core_
import pyfltr.command.executor
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model


def make_skipped_result(
    command: str,
    config: pyfltr.config.model.Config,
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


@dataclasses.dataclass
class StageCallbacks:
    """表示と中断を実行制御へ渡すためのコールバック。"""

    archive: collections.abc.Callable[[pyfltr.command.core_.CommandResult], None] | None = None
    on_result: collections.abc.Callable[[pyfltr.command.core_.CommandResult], None] | None = None
    is_interrupted: collections.abc.Callable[[], bool] = lambda: False
    on_interrupted: collections.abc.Callable[[str], None] | None = None
    replace_result: (
        collections.abc.Callable[[pyfltr.command.core_.CommandResult, str], pyfltr.command.core_.CommandResult] | None
    ) = None
    on_skipped: collections.abc.Callable[[str], None] | None = None


def run_stages(
    commands: list[str],
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    execute: collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult],
    *,
    include_fix_stage: bool = False,
    fail_fast: bool = False,
    callbacks: StageCallbacks | None = None,
    results: list[pyfltr.command.core_.CommandResult] | None = None,
) -> list[pyfltr.command.core_.CommandResult]:
    """fix、直列formatter、LPT順の並列lint・testを共通の制御で実行する。"""
    hooks = callbacks if callbacks is not None else StageCallbacks()
    collected = results if results is not None else []
    config = base_ctx.config
    fixers, formatters, parallel = pyfltr.command.executor.split_commands_for_execution(
        commands,
        config,
        base_ctx.all_files,
        include_fix_stage=include_fix_stage,
        subproject_configs=base_ctx.subproject_configs,
    )

    def archive(result: pyfltr.command.core_.CommandResult) -> None:
        if hooks.archive is not None and not result.cached:
            hooks.archive(result)

    def record(result: pyfltr.command.core_.CommandResult, stage: str) -> None:
        if hooks.replace_result is not None:
            result = hooks.replace_result(result, stage)
        collected.append(result)
        archive(result)
        if hooks.on_result is not None:
            hooks.on_result(result)

    def skip(remaining: list[str]) -> None:
        pyfltr.command.process.terminate_active_processes()
        reason = "Ctrl+C により中断しました。" if hooks.is_interrupted() else None
        for command in remaining:
            record(make_skipped_result(command, config, reason=reason), "skipped")
            if hooks.on_skipped is not None:
                hooks.on_skipped(command)

    for command in fixers:
        result = execute(command, True)
        archive(result)
        if hooks.is_interrupted():
            if hooks.on_interrupted is not None:
                hooks.on_interrupted(command)
            skip([*formatters, *parallel])
            return collected
        if fail_fast and result.failed:
            skip([*formatters, *parallel])
            return collected

    for index, command in enumerate(formatters):
        result = execute(command, False)
        record(result, "formatter")
        if hooks.is_interrupted() or (fail_fast and result.failed):
            skip([*formatters[index + 1 :], *parallel])
            return collected

    if parallel:
        aborted = False
        aborted_commands: set[str] = set()
        with concurrent.futures.ThreadPoolExecutor(max_workers=config["jobs"]) as executor:
            future_to_command = {executor.submit(execute, command, False): command for command in parallel}
            for future in concurrent.futures.as_completed(future_to_command):
                command = future_to_command[future]
                try:
                    result = future.result()
                except concurrent.futures.CancelledError:
                    aborted_commands.add(command)
                    continue
                record(result, "parallel")
                if fail_fast and not aborted and result.failed:
                    aborted = True
                    cancel_pending_futures(future_to_command, aborted_commands)
                    pyfltr.command.process.terminate_active_processes()
                if hooks.is_interrupted():
                    cancel_pending_futures(future_to_command, aborted_commands)
        if aborted_commands:
            skip(list(aborted_commands))
    return collected
