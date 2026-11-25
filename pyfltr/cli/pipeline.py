from __future__ import annotations

# TUI専用のtextualを読み込む`pyfltr.output.ui`は、TUIを使う実行だけで読み込む。
# Python 3.15以上で遅延importとなり、それ未満の版では通常のimportとして扱われる（PEP 810）。
__lazy_modules__ = ["pyfltr.output.ui", "pyfltr.verification_sync"]

import argparse
import dataclasses
import importlib.metadata
import logging
import os
import pathlib
import shlex
import subprocess
import sys
import typing

import pyfltr.cli.command_selection
import pyfltr.cli.output_format
import pyfltr.cli.overrides
import pyfltr.cli.pipeline_targets
import pyfltr.cli.run_storage
import pyfltr.command.completion
import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.execution_slot
import pyfltr.command.executor
import pyfltr.command.only_failed
import pyfltr.command.precommit_guidance
import pyfltr.command.stage_runner
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection
import pyfltr.output.formatters
import pyfltr.output.heartbeat
import pyfltr.output.logging_
import pyfltr.output.render
import pyfltr.output.ui
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.state.retry
import pyfltr.verification_sync
import pyfltr.warnings_

logger = logging.getLogger(__name__)

# text_logger / structured_logger / text_output_lock は output/logging_.py から参照する。
text_logger = pyfltr.output.logging_.text_logger
structured_logger = pyfltr.output.logging_.structured_logger
lock = pyfltr.output.logging_.text_output_lock


def can_use_ui() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


class PipelineOutcome(typing.NamedTuple):
    exit_code: int
    """終了コード。0 = 成功、1 = 失敗。"""
    run_id: str | None
    """実行アーカイブの`run_id`。アーカイブ無効・採番失敗・early exit時は`None`。"""
    completion: pyfltr.command.completion.RunCompletion
    """完了判定。全経路で確定し、MCP応答とJSONL `summary`の両方へ同じ値を渡す。"""
    results: tuple[pyfltr.command.core_.CommandResult, ...] = ()
    """キャッシュ復元を含む通常ステージの最終結果。"""


def _slot_progress_text_emit(message: str) -> None:
    with lock:
        text_logger.warning(message)


def _make_attach_retry_command(
    *,
    retry_args_template: list[str],
    launcher_prefix: list[str],
    target_base_cwd: str,
) -> typing.Callable[[pyfltr.command.core_.CommandResult], None]:

    def _hook(result: pyfltr.command.core_.CommandResult) -> None:
        pyfltr.state.retry.populate_retry_command(
            result,
            retry_args_template=retry_args_template,
            launcher_prefix=launcher_prefix,
            target_base_cwd=target_base_cwd,
        )

    return _hook


def run_commands_with_cli(
    commands: list[str],
    args: pyfltr.run_options.RunOptions,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    per_command_log: bool,
    include_fix_stage: bool = False,
    on_result: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    slot_wait_hook: pyfltr.command.execution_slot.WaitIntervalCallback | None = None,
    fail_fast: bool = False,
    only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None = None,
    heartbeat: pyfltr.output.heartbeat.HeartbeatMonitor | None = None,
) -> list[pyfltr.command.core_.CommandResult]:

    def execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        return _run_one_command(
            command,
            args,
            base_ctx,
            per_command_log=per_command_log,
            fix_stage=fix_stage,
            only_failed_targets=pyfltr.command.targets.pick_targets(only_failed_targets, command),
            heartbeat=heartbeat,
        )

    def record(result: pyfltr.command.core_.CommandResult) -> None:
        def emit() -> None:
            if on_result is not None:
                on_result(result)

        if heartbeat is None:
            emit()
        else:
            heartbeat.finalize(result.command, emit)

    return pyfltr.command.stage_runner.run_stages(
        commands,
        base_ctx,
        execute,
        include_fix_stage=include_fix_stage,
        fail_fast=fail_fast,
        callbacks=pyfltr.command.stage_runner.StageCallbacks(
            archive=archive_hook, on_result=record, on_slot_wait=slot_wait_hook, on_slot_progress=_slot_progress_text_emit
        ),
    )


def _run_one_command(
    command: str,
    args: pyfltr.run_options.RunOptions,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    per_command_log: bool,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.command.only_failed.ToolTargets | None = None,
    heartbeat: pyfltr.output.heartbeat.HeartbeatMonitor | None = None,
) -> pyfltr.command.core_.CommandResult:
    with pyfltr.command.executor.serial_group_lock(base_ctx.config.commands[command].serial_group):
        with lock:
            suffix = " (fix)" if fix_stage else ""
            text_logger.info(f"{command}{suffix} 実行中です...")
        on_start: typing.Callable[[], None] | None = None
        on_end: typing.Callable[[], None] | None = None
        if heartbeat is not None:

            def _on_start(_command: str = command) -> None:
                heartbeat.on_command_start(_command)

            def _on_end(_command: str = command) -> None:
                heartbeat.on_command_end(_command)

            on_start = _on_start
            on_end = _on_end
        ctx = pyfltr.command.core_.ExecutionContext(
            base=base_ctx,
            fix_stage=fix_stage,
            only_failed_targets=only_failed_targets,
            on_subprocess_start=on_start,
            on_subprocess_end=on_end,
        )
        result = pyfltr.command.dispatcher.execute_command(command, args, ctx)
        if per_command_log:
            use_ga = (getattr(args, "output_format", "text") or "text") == "github-annotations"
            pyfltr.output.render.write_log(result, use_github_annotations=use_ga)
        else:
            with lock:
                text_logger.info(f"{command}{suffix} 完了 ({result.get_status_text()})")
        return result


def run_pipeline(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    config: pyfltr.config.model.Config,
    *,
    start_cwd: pathlib.Path | None = None,
    original_sys_args: list[str] | None = None,
    force_text_on_stderr: bool = False,
    jsonl_warnings_reach_consumer: bool = True,
    archive_required: bool = False,
) -> PipelineOutcome:
    """出力形式に応じた警告配送スコープを設定してパイプラインを実行する。"""
    if (args.output_format or "text") == "jsonl":
        with pyfltr.warnings_.defer_stderr():
            return _run_pipeline(
                args,
                commands,
                config,
                start_cwd=start_cwd,
                original_sys_args=original_sys_args,
                force_text_on_stderr=force_text_on_stderr,
                jsonl_warnings_reach_consumer=jsonl_warnings_reach_consumer,
                archive_required=archive_required,
            )
    return _run_pipeline(
        args,
        commands,
        config,
        start_cwd=start_cwd,
        original_sys_args=original_sys_args,
        force_text_on_stderr=force_text_on_stderr,
        jsonl_warnings_reach_consumer=jsonl_warnings_reach_consumer,
        archive_required=archive_required,
    )


def _run_pipeline(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    config: pyfltr.config.model.Config,
    *,
    start_cwd: pathlib.Path | None = None,
    original_sys_args: list[str] | None = None,
    force_text_on_stderr: bool = False,
    jsonl_warnings_reach_consumer: bool = True,
    archive_required: bool = False,
) -> PipelineOutcome:
    start_cwd_path = start_cwd if start_cwd is not None else pathlib.Path.cwd()
    formatter, early_ctx, structured_stdout = _initialize_output(
        args,
        config,
        force_text_on_stderr=force_text_on_stderr,
        jsonl_warnings_reach_consumer=jsonl_warnings_reach_consumer,
    )
    prepared = pyfltr.cli.pipeline_targets.prepare_execution_targets(
        args, commands, config, start_cwd_path, archive_required=archive_required
    )
    if isinstance(prepared, pyfltr.cli.pipeline_targets.TargetPreparationExit):
        returncode = 0 if prepared.reason == "only-failed" else 1
        completion = _evaluate_completion([], args, config, files_reached=0, unmet_commands=[])
        early_ctx = dataclasses.replace(early_ctx, completion=completion)
        formatter.on_start(early_ctx)
        formatter.on_finish(early_ctx, [], returncode, pyfltr.warnings_.collected_warnings())
        return PipelineOutcome(returncode, None, completion)

    effective_sys_args = list(original_sys_args) if original_sys_args is not None else list(sys.argv[1:])
    launcher_prefix = pyfltr.state.retry.detect_launcher_prefix()
    retry_args_template = pyfltr.state.retry.build_retry_args_template(effective_sys_args)
    archive_store, run_id = pyfltr.cli.run_storage.initialize_archive(
        args, config, prepared.commands, prepared.all_files, start_cwd_path, archive_required=archive_required
    )
    base_ctx: pyfltr.command.core_.ExecutionBaseContext | None = None
    try:
        _log_runtime(start_cwd_path, run_id, launcher_prefix)
        cache_store = pyfltr.cli.run_storage.initialize_cache(args, config)
        archive_hook = (
            pyfltr.cli.run_storage.make_archive_hook(archive_store, run_id, archive_required=archive_required)
            if archive_store is not None and run_id is not None
            else None
        )
        slot_wait_hook = (
            pyfltr.cli.run_storage.make_slot_wait_hook(archive_store, run_id, archive_required=archive_required)
            if archive_store is not None and run_id is not None
            else None
        )
        attach_retry_command = _make_attach_retry_command(
            retry_args_template=retry_args_template,
            launcher_prefix=launcher_prefix,
            target_base_cwd=str(start_cwd_path),
        )
        base_ctx = _prepare_execution_context(
            config,
            prepared,
            start_cwd_path,
            cache_store,
            run_id,
            fail_fast=bool(getattr(args, "fail_fast", False)),
            exclude_always_targets=bool(getattr(args, "no_always_targets", False)),
        )
        ctx = _prepare_run_output(
            args,
            formatter,
            early_ctx,
            prepared,
            run_id,
            launcher_prefix,
            retry_args_template,
            structured_stdout,
        )
        results, returncode, ctx, interruption = _execute_pipeline_commands(
            args,
            prepared.commands,
            base_ctx,
            formatter,
            ctx,
            archive_hook=archive_hook,
            slot_wait_hook=slot_wait_hook,
            attach_retry_command=attach_retry_command,
            only_failed_targets=prepared.only_failed_targets,
        )
        return _finish_pipeline(
            args,
            base_ctx,
            formatter,
            ctx,
            results,
            returncode,
            archive_store,
            prepared.unmet,
            archive_required=archive_required,
            interruption=interruption,
        )
    except KeyboardInterrupt:
        pyfltr.cli.run_storage.interrupt_archive(archive_store, run_id, "keyboard_interrupt")
        raise
    finally:
        try:
            if base_ctx is not None:
                base_ctx.cleanup()
        finally:
            if archive_store is not None and run_id is not None:
                archive_store.release_run(run_id)


def _initialize_output(
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    *,
    force_text_on_stderr: bool,
    jsonl_warnings_reach_consumer: bool,
) -> tuple[pyfltr.output.formatters.OutputFormatter, pyfltr.output.formatters.RunOutputContext, bool]:
    output_format = args.output_format or "text"
    format_source: str | None = getattr(args, "format_source", None)
    output_file: pathlib.Path | None = args.output_file
    structured_stdout = output_format in ("jsonl", "sarif", "code-quality") and output_file is None
    if structured_stdout:
        args.ui = None
        args.no_ui = True
        args.no_clear = True
        args.stream = False

    formatter = pyfltr.output.formatters.FORMATTERS[output_format]()

    early_ctx = pyfltr.output.formatters.RunOutputContext(
        config=config,
        output_file=output_file,
        force_text_on_stderr=force_text_on_stderr,
    )
    formatter.configure_loggers(early_ctx)
    if not jsonl_warnings_reach_consumer:
        pyfltr.output.logging_.configure_structured_output(None)

    if not args.no_clear:
        clear_cmd = ["cmd", "/c", "cls"] if os.name == "nt" else ["clear"]
        subprocess.run(clear_cmd, check=False)

    quiet = bool(getattr(args, "quiet", False))
    early_run_ctx = pyfltr.output.formatters.RunOutputContext(
        config=config,
        output_file=output_file,
        force_text_on_stderr=force_text_on_stderr,
        commands=[],
        all_files=0,
        format_source=format_source,
        quiet=quiet,
        subcommand=getattr(args, "subcommand", None),
        jsonl_warnings_reach_consumer=jsonl_warnings_reach_consumer,
    )
    return formatter, early_run_ctx, structured_stdout


def _log_runtime(start_cwd_path: pathlib.Path, run_id: str | None, launcher_prefix: list[str]) -> None:
    text_logger.info(f"{'-' * 10} pyfltr {'-' * (72 - 10 - 8)}")
    text_logger.info(f"version:        {importlib.metadata.version('pyfltr')}")
    text_logger.info(f"sys.executable: {sys.executable}")
    text_logger.info(f"sys.version:    {sys.version}")
    text_logger.info(f"cwd:            {start_cwd_path}")
    if run_id is not None:
        launcher_cmd = shlex.join(launcher_prefix)
        text_logger.info("run_id:         %s(`%s show-run %s` で詳細を確認可能)", run_id, launcher_cmd, run_id)
    text_logger.info("-" * 72)


def _prepare_execution_context(
    config: pyfltr.config.model.Config,
    prepared: pyfltr.cli.pipeline_targets.PreparedTargets,
    start_cwd_path: pathlib.Path,
    cache_store: pyfltr.state.cache.CacheStore | None,
    run_id: str | None,
    *,
    fail_fast: bool,
    exclude_always_targets: bool,
) -> pyfltr.command.core_.ExecutionBaseContext:
    base_ctx = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=prepared.all_files,
        cache_store=cache_store,
        cache_run_id=run_id,
        start_cwd=start_cwd_path,
        subprojects=prepared.subprojects,
        subproject_files=prepared.subproject_files,
        external_files=prepared.external_files,
        subproject_configs=prepared.subproject_configs,
        fail_fast=fail_fast,
        exclude_always_targets=exclude_always_targets,
    )
    if "pytest" in prepared.commands:
        base_ctx.pytest_base = pyfltr.cli.pipeline_targets.build_pytest_base(base_ctx, fast_selected=prepared.fast_selected)

    return base_ctx


def _prepare_run_output(
    args: pyfltr.run_options.RunOptions,
    formatter: pyfltr.output.formatters.OutputFormatter,
    early_ctx: pyfltr.output.formatters.RunOutputContext,
    prepared: pyfltr.cli.pipeline_targets.PreparedTargets,
    run_id: str | None,
    launcher_prefix: list[str],
    retry_args_template: list[str],
    structured_stdout: bool,
) -> pyfltr.output.formatters.RunOutputContext:
    per_command_log = bool(args.stream)
    include_details_from_stream = not per_command_log
    ctx = dataclasses.replace(
        early_ctx,
        commands=prepared.commands,
        all_files=len(prepared.all_files),
        run_id=run_id,
        launcher_prefix=launcher_prefix,
        retry_args_template=retry_args_template,
        stream=per_command_log,
        include_details=include_details_from_stream,
        structured_stdout=structured_stdout,
    )

    formatter.on_start(ctx)

    return ctx


def _execute_pipeline_commands(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    formatter: pyfltr.output.formatters.OutputFormatter,
    ctx: pyfltr.output.formatters.RunOutputContext,
    *,
    archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None,
    slot_wait_hook: pyfltr.command.execution_slot.WaitIntervalCallback | None,
    attach_retry_command: typing.Callable[[pyfltr.command.core_.CommandResult], None],
    only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None,
) -> tuple[
    list[pyfltr.command.core_.CommandResult],
    int,
    pyfltr.output.formatters.RunOutputContext,
    pyfltr.output.ui.UIInterruption | None,
]:
    use_ui = not args.no_ui and (args.ui or can_use_ui())
    heartbeat: pyfltr.output.heartbeat.HeartbeatMonitor | None = None
    if args.output_format == "jsonl" and not use_ui and ctx.jsonl_warnings_reach_consumer:
        heartbeat = pyfltr.output.heartbeat.HeartbeatMonitor()
        heartbeat.start()

    composed_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None
    if archive_hook is not None:

        def _composed_archive_hook(result: pyfltr.command.core_.CommandResult) -> None:
            attach_retry_command(result)
            archive_hook(result)

        composed_hook = _composed_archive_hook
    else:
        composed_hook = attach_retry_command

    def _on_result_callback(result: pyfltr.command.core_.CommandResult) -> None:
        formatter.on_result(ctx, result)

    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    fail_fast = bool(getattr(args, "fail_fast", False))
    interruption = None
    try:
        if use_ui:
            results, returncode, interruption = pyfltr.output.ui.run_commands_with_ui(
                commands,
                args,
                base_ctx,
                archive_hook=composed_hook,
                slot_wait_hook=slot_wait_hook,
                on_result=_on_result_callback,
                fail_fast=fail_fast,
                only_failed_targets=only_failed_targets,
            )
            ctx = dataclasses.replace(ctx, stream=False, include_details=True)
        else:
            results = run_commands_with_cli(
                commands,
                args,
                base_ctx,
                per_command_log=ctx.stream,
                include_fix_stage=include_fix_stage,
                on_result=_on_result_callback,
                archive_hook=composed_hook,
                slot_wait_hook=slot_wait_hook,
                fail_fast=fail_fast,
                only_failed_targets=only_failed_targets,
                heartbeat=heartbeat,
            )
            returncode = 0

    finally:
        if heartbeat is not None:
            heartbeat.stop()
    return results, returncode, ctx, interruption


def _finish_pipeline(
    args: pyfltr.run_options.RunOptions,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    formatter: pyfltr.output.formatters.OutputFormatter,
    ctx: pyfltr.output.formatters.RunOutputContext,
    results: list[pyfltr.command.core_.CommandResult],
    returncode: int,
    archive_store: pyfltr.state.archive.ArchiveStore | None,
    unmet: list[str],
    *,
    archive_required: bool = False,
    interruption: pyfltr.output.ui.UIInterruption | None = None,
) -> PipelineOutcome:
    if interruption == "confirmed":
        pyfltr.cli.run_storage.interrupt_archive(archive_store, ctx.run_id, "keyboard_interrupt")

    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)

    if (
        returncode == 0
        and args.targets
        and pyfltr.warnings_.filtered_direct_files(reason="missing")
        and results
        and all(result.skipped for result in results)
    ):
        returncode = 1

    completion = _evaluate_completion(
        results, args, base_ctx.config, files_reached=len(base_ctx.all_files), unmet_commands=unmet
    )
    ctx = dataclasses.replace(ctx, completion=completion)
    formatter.on_finish(ctx, results, returncode, pyfltr.warnings_.collected_warnings())

    if interruption is None:
        pyfltr.cli.run_storage.finalize_archive(
            archive_store, ctx.run_id, returncode, ctx.commands, ctx.all_files, archive_required=archive_required
        )

    _maybe_emit_precommit_guidance(results, structured_stdout=ctx.structured_stdout, quiet=ctx.quiet)

    return PipelineOutcome(returncode, ctx.run_id, completion, tuple(results))


def _evaluate_completion(
    results: list[pyfltr.command.core_.CommandResult],
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    *,
    files_reached: int,
    unmet_commands: list[str],
) -> pyfltr.command.completion.RunCompletion:
    raw_commands = getattr(args, "commands", None)
    requested = (
        [name for name in pyfltr.cli.command_selection.flatten_commands_arg(raw_commands, config) if name in config.commands]
        if raw_commands is not None
        else []
    )
    return pyfltr.command.completion.evaluate_completion(
        results,
        config,
        files_reached=files_reached,
        requested_commands=requested,
        unmet_commands=unmet_commands,
        missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
        fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
    )


_PRECOMMIT_MM_MESSAGE: str = (
    "formatterによる自動修正が発生しました。"
    "`git status`で変更を確認し、必要なら`git add`してから`git commit`を再実行してください。"
)


def _maybe_emit_precommit_guidance(
    results: list[pyfltr.command.core_.CommandResult],
    *,
    structured_stdout: bool,
    quiet: bool = False,
) -> None:
    if quiet or structured_stdout:
        return
    if not any(result.formatted for result in results):
        return
    if not pyfltr.command.precommit_guidance.is_invoked_from_git_commit():
        return
    print(_PRECOMMIT_MM_MESSAGE, file=sys.stderr)


def calculate_returncode(results: list[pyfltr.command.core_.CommandResult], exit_zero_even_if_formatted: bool) -> int:
    status = pyfltr.command.core_.overall_status(results)
    return int(status == "FAILED" or (status == "FORMATTED" and not exit_zero_even_if_formatted))


_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(pyfltr.output.formatters.FORMATTERS.keys())


def load_run_config(config_dir: pathlib.Path | None = None, *, archive_required: bool = False) -> pyfltr.config.model.Config:
    config = pyfltr.config.config.load_config(config_dir=config_dir)
    if archive_required:
        config.values["archive"] = True
    return config


def prepare_run(
    options: pyfltr.run_options.RunOptions,
    *,
    config: pyfltr.config.model.Config | None = None,
    archive_required: bool = False,
    list_commands: str = pyfltr.cli.command_selection.CLI_LIST_COMMANDS,
) -> tuple[pyfltr.config.model.Config, list[str]]:
    if config is None:
        config = load_run_config(options.work_dir, archive_required=archive_required)
    pyfltr.cli.overrides.apply_cli_overrides(config, options)
    commands = pyfltr.config.selection.resolve_aliases(
        pyfltr.cli.command_selection.flatten_commands_arg(options.commands, config), config
    )
    pyfltr.cli.command_selection.validate_commands(commands, config, list_commands=list_commands)
    return config, commands


def _resolve_output_format(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> pyfltr.cli.output_format.OutputFormatResolution:
    subcommand_default = "jsonl" if args.subcommand == "run-for-agent" else None
    return pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=_VALID_OUTPUT_FORMATS,
        subcommand_default=subcommand_default,
        ai_agent_default="jsonl",
    )


def run_impl(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    _original_sys_args: typing.Sequence[str],
    resolved_targets: list[pathlib.Path] | None,
    *,
    original_cwd: str,
    reparse_fn: typing.Callable[[list[str]], tuple[argparse.ArgumentParser, argparse.Namespace]] | None = None,
) -> int:
    pyfltr.warnings_.clear()

    if args.ci:
        args.shuffle = False
        args.no_ui = True

    if getattr(args, "from_run", None) is not None and not getattr(args, "only_failed", False):
        parser.error("argument --from-run: requires --only-failed")

    if args.ui and args.no_ui:
        parser.error("--ui と --no-ui は同時に指定できません。どちらか一方を指定してください。")

    if args.version:
        print(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    subcommand = getattr(args, "subcommand", None)
    if subcommand in {"ci", "fast"}:
        cwd = args.work_dir if args.work_dir is not None else pathlib.Path(original_cwd)
        try:
            sync_diagnostics = pyfltr.verification_sync.check(cwd, staged=subcommand == "fast")
        except (OSError, subprocess.CalledProcessError) as e:
            logger.error(f"verification-sync: {e}")
            return 1
        if sync_diagnostics:
            for diagnostic in sync_diagnostics:
                logger.error(f"verification-sync: {diagnostic}")
            return 1

    resolution = _resolve_output_format(parser, args)
    output_format, format_source = resolution.format, resolution.source
    output_file: pathlib.Path | None = args.output_file

    try:
        config = load_run_config(args.work_dir)
    except (ValueError, OSError) as e:
        logger.error(f"設定エラー: {pyfltr.config.config.format_config_error(e, retry='元の実行コマンド')}")
        return 1

    args.output_format = output_format
    args.format_source = format_source
    args.output_file = output_file
    work_dir = args.work_dir

    custom_commands = [name for name, info in config.commands.items() if not info.builtin]
    if custom_commands and reparse_fn is not None:
        parser, args = reparse_fn(custom_commands)
        args.output_format = output_format
        args.format_source = format_source
        args.output_file = output_file
        args.work_dir = work_dir
        if getattr(args, "no_fix", False):
            args.include_fix_stage = False
        if args.ci:
            args.shuffle = False
            args.no_ui = True

    if resolved_targets is not None:
        args.targets = resolved_targets

    options = pyfltr.run_options.RunOptions.from_values(vars(args))

    try:
        config, commands = prepare_run(options, config=config)
    except ValueError as e:
        parser.error(str(e))

    outcome = run_pipeline(
        options,
        commands,
        config,
        start_cwd=options.work_dir if options.work_dir is not None else pathlib.Path(original_cwd),
        original_sys_args=options.retry_arguments(),
    )
    return outcome.exit_code
