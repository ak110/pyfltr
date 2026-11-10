"""パイプライン実行と結果整形。

非TUI経路でのコマンド実行（`run_commands_with_cli`）と、
パイプライン全体を駆動する`run_impl`・`run_pipeline`・
`calculate_returncode`を担う。
text整形描画（`render_results` / `write_log`）は`output/render.py`に分離している。
"""

from __future__ import annotations

# TUI専用のtextualを読み込む`pyfltr.output.ui`は、TUIを使う実行だけで読み込む。
# Python 3.15以上で遅延importとなり、それ未満の版では通常のimportとして扱われる（PEP 810）。
__lazy_modules__ = ["pyfltr.output.ui"]

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
import pyfltr.warnings_

logger = logging.getLogger(__name__)

# text_logger / structured_logger / text_output_lock は output/logging_.py から参照する。
text_logger = pyfltr.output.logging_.text_logger
structured_logger = pyfltr.output.logging_.structured_logger
lock = pyfltr.output.logging_.text_output_lock


def can_use_ui() -> bool:
    """UIを使用するかどうか判定。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


class PipelineOutcome(typing.NamedTuple):
    """`run_pipeline`の戻り値。"""

    exit_code: int
    """終了コード。0 = 成功、1 = 失敗。"""
    run_id: str | None
    """実行アーカイブの`run_id`。アーカイブ無効・採番失敗・early exit時は`None`。"""
    completion: pyfltr.command.completion.RunCompletion
    """完了判定。全経路で確定し、MCP応答とJSONL `summary`の両方へ同じ値を渡す。"""
    results: tuple[pyfltr.command.core_.CommandResult, ...] = ()
    """キャッシュ復元を含む通常ステージの最終結果。"""


def _slot_progress_text_emit(message: str) -> None:
    """実行枠の待機の継続・再開をtext_logger経由で逐次出力する。

    textではstdout、jsonl等ではstderrへ出る（出力先は`pyfltr.output.heartbeat._heartbeat_text_emit`と同じ）。
    待機中は実行中のツールが無く`pyfltr.output.heartbeat.HeartbeatMonitor`も発火しないため、この出力が停止との区別の手掛かりになる。
    """
    with lock:
        text_logger.warning(message)


def _make_attach_retry_command(
    *,
    retry_args_template: list[str],
    launcher_prefix: list[str],
    target_base_cwd: str,
) -> typing.Callable[[pyfltr.command.core_.CommandResult], None]:
    """retry_command付与フックを生成する。

    各ツール完了時（archive_hookと同じタイミング）に呼ばれるon_result経路へ挿入し、
    `populate_retry_command`（失敗ファイルフィルタリング・cached判定を含む）に委譲する。
    """

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
    """コマンドを実行する (非 TUI)。

    `per_command_log=True`のときは各コマンド完了時に詳細ログを即時出力する（`--stream`相当）。
    `per_command_log=False`のときは完了時に1行進捗のみを出力し、詳細はバッファに残す。
    いずれの場合も、呼び出し側で最後に`render_results()`を呼ぶことで
    summaryと詳細ログをまとめて出力できる。

    `include_fix_stage=True`のとき、fix-args定義済みコマンドを先に`--fix`付きで
    直列実行してから、formatter → linter/testerの順で通常実行に進む
    （`ruff check --fix → ruff format → ruff check`と同じ2段階方式の一般化）。

    `on_result`が指定されている場合、各コマンド完了時にコールバックを呼び出す。
    JSONL stdoutモードでのストリーミング出力に使用する。

    `slot_wait_hook`が指定されている場合、実行枠の待機区間の開始と終了を渡す。

    `archive_hook`が指定されている場合、各コマンド完了時に実行アーカイブへ書き込む。
    成功したfixステージの結果はsummaryに含めず、アーカイブへの保存だけを行う。
    失敗したfixステージの結果は同じツールの通常ステージの結果へ統合して
    summary・`on_result`・アーカイブへ渡す（`pyfltr.command.stage_runner.run_stages`を参照）。

    `base_ctx`はパイプライン全体で不変のコンテキスト（config・all_files・cache_store・
    cache_run_idを含む）。各コマンド実行前に`ExecutionContext`を組み立てて渡す。

    `fail_fast=True`のとき、いずれかのツールが`status`として`failed`または`resolution_failed`を返した
    時点で未開始のジョブを`future.cancel()`で打ち切り、起動済みサブプロセスに
    `terminate()`を送る。formatterの`formatted`はfailureに含めない。

    `only_failed_targets`が指定された場合、ツール別の`ToolTargets`を
    `execute_command`へ渡す（`--only-failed`経路）。実対象の決め方は
    `ToolTargets.resolve_files()`に従う。
    """

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
    """1 コマンドの実行。

    `per_command_log=True`ならば完了直後に詳細ログを`write_log()`で出力する。
    それ以外は開始/完了の1行進捗のみ出力する。
    `heartbeat` が指定された場合、subprocess起動・終了に合わせて実行中コマンド集合を更新し、
    パイプラインheartbeatの追跡対象に含める。
    """
    # serial_groupを持つコマンドは同一グループ内で排他実行される（cargo / dotnet等）
    with pyfltr.command.executor.serial_group_lock(base_ctx.config.commands[command].serial_group):
        with lock:
            suffix = " (fix)" if fix_stage else ""
            text_logger.info(f"{command}{suffix} 実行中です...")
        on_start: typing.Callable[[], None] | None = None
        on_end: typing.Callable[[], None] | None = None
        if heartbeat is not None:

            def _on_start(_command: str = command) -> None:
                # type checkerにcaptureを明示するためデフォルト引数で固定する。
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
    """実行パイプライン。

    `force_text_on_stderr=True` を渡すと、人間向けtext整形ログの出力先を
    stdoutではなくstderrに強制する（MCP経路でstdoutをJSON-RPCフレームが
    占有するケース用）。

    Returns:
        `PipelineOutcome`（`exit_code`・`run_id`・`completion`）。
        `exit_code` は0 = 成功、1 = 失敗。
        `run_id` は実行アーカイブが有効で採番に成功した場合のULID文字列、
        無効・採番失敗・early exit時は `None`。
        `--only-failed` 指定で「直前runなし」「失敗ツールなし」「対象ファイル
        交差が空」のいずれかに該当する場合はearly exitとして `exit_code=0`・`run_id=None` を
        返す。`run_id`はアーカイブの参照キーであり、実行の有無は表さない。
        アーカイブ初期化に失敗した実行でも`None`になる。
        `completion` はearly exitを含む全経路で確定し、`formatter.on_finish` より前に
        出力文脈へ渡してJSONL `summary` と同じ値にする。

    戻り値で`run_id`を返すのはMCP経路がrun_idを確実に取得するため。
    代替案としてMCP側で `ArchiveStore.list_runs(limit=1)` を引く案も検討
    したが、同一ユーザーキャッシュを参照する並行プロセスがあると別runの
    `run_id` を誤って拾うリスクがあるため戻り値経由とした。

    heartbeat監視 (`pyfltr.output.heartbeat.HeartbeatMonitor`) は出力形式が `jsonl` のときに限定して起動する。
    SARIFとCode Qualityは `on_finish` で単一JSONドキュメントを一括出力するbuffering型
    formatterのため、途中で `status:"running"` レコードを混入させると最終的な出力
    （SARIF 2.1.0オブジェクト・Code Climate JSON配列）が破損する。
    `text` 等のJSONLレコードが流れない出力形式ではheartbeatの観測対象が成立しない。
    TUI経路はUIに進捗表示があるためheartbeat不要。
    """
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
            # 対象ファイルは実行起点相対で保持されるため、起動時のcwdではなく実行起点を基準に絶対化する。
            target_base_cwd=str(start_cwd_path),
        )
        base_ctx = _prepare_execution_context(
            config, prepared, start_cwd_path, cache_store, run_id, fail_fast=bool(getattr(args, "fail_fast", False))
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
        results, returncode, ctx = _execute_pipeline_commands(
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
        )
    finally:
        # 例外（Ctrl+Cを含む）で抜けた経路でも一時ディレクトリと稼働中のロックを残さない。
        # 正常終了ではロックを`finalize_run`が解放済みのため、`release_run`は何もしない。
        if base_ctx is not None:
            base_ctx.cleanup()
        if archive_store is not None and run_id is not None:
            archive_store.release_run(run_id)


def _initialize_output(
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    *,
    force_text_on_stderr: bool,
    jsonl_warnings_reach_consumer: bool,
) -> tuple[pyfltr.output.formatters.OutputFormatter, pyfltr.output.formatters.RunOutputContext, bool]:
    """出力形式・logger・early exit時の出力文脈を初期化する。"""
    output_format = args.output_format or "text"
    format_source: str | None = getattr(args, "format_source", None)
    output_file: pathlib.Path | None = args.output_file
    # JSONL / SARIF / code-qualityのstdoutモードではstdoutを構造化出力が占有するため、
    # UI・画面クリア・streamによる詳細ログ即時出力を無効化する。
    structured_stdout = output_format in ("jsonl", "sarif", "code-quality") and output_file is None
    if structured_stdout:
        args.ui = None
        args.no_ui = True
        args.no_clear = True
        args.stream = False

    formatter = pyfltr.output.formatters.FORMATTERS[output_format]()

    # loggerを初期化する。同一プロセスでrun_pipelineが複数回呼ばれるMCP経路でも、
    # format / output_file / force_text_on_stderrの組み合わせで出力先が変わるため、毎回再設定する。
    # configure_loggersはoutput_file / force_text_on_stderrのみ参照するため、
    # run_id等が未確定の段階でも呼び出せる（残フィールドはデフォルト値のまま渡す）。
    early_ctx = pyfltr.output.formatters.RunOutputContext(
        config=config,
        output_file=output_file,
        force_text_on_stderr=force_text_on_stderr,
    )
    formatter.configure_loggers(early_ctx)
    if not jsonl_warnings_reach_consumer:
        pyfltr.output.logging_.configure_structured_output(None)

    # ターミナルをクリア
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
    """実行環境を採番したrun_idとともにtext出力へ通知する。"""
    # 実行環境の情報を出力（run_id採番後にまとめて出力することで区切り線内に含める）。
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
) -> pyfltr.command.core_.ExecutionBaseContext:
    """対象解決の結果から通常実行とfast pytestの実行基盤を構築する。"""
    # run_pipelineが1回だけ組み立てる不変コンテキスト。
    # archive_storeはhook経由で渡すためContextには含めない。
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
    """対象とアーカイブ情報を出力文脈へ渡し、開始を通知する。"""
    # 各ツール完了時のフック: retry_command付与 → archive書き込み → formatter.on_result （ストリーミング等）。
    # retry_commandはarchiveとJSONL streamingの双方で必要になるため、archive_hookより前に挿入する。
    # formatter.on_resultはarchive_hookの後に呼ぶ（result.archived=Trueが設定された後）。
    # on_start / on_result / on_finishで使う完全なctxを構築する。
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
) -> tuple[list[pyfltr.command.core_.CommandResult], int, pyfltr.output.formatters.RunOutputContext]:
    """CLI/TUIの実行と結果フックを接続し、heartbeatの生存期間を管理する。"""
    use_ui = not args.no_ui and (args.ui or can_use_ui())
    # heartbeat監視の起動。
    # `jsonl`形式のときに限定して起動する。
    # `sarif`・`code-quality`はbufferingformatter（`on_finish`で単一JSONドキュメントを一括出力）のため、
    # 途中で`status:"running"`レコードを混入させると最終的な出力（SARIF 2.1.0オブジェクト・
    # Code Climate JSON配列）が不正な形式になる。
    # `text`等のJSONLレコードが流れない出力形式ではheartbeatの観測対象が成立しない。
    # TUI経路はUIに進捗表示があるため別途heartbeat不要。
    heartbeat: pyfltr.output.heartbeat.HeartbeatMonitor | None = None
    if args.output_format == "jsonl" and not use_ui and ctx.jsonl_warnings_reach_consumer:
        heartbeat = pyfltr.output.heartbeat.HeartbeatMonitor()
        heartbeat.start()

    # 各ツール完了時のフック順序:
    #   1. attach_retry_command(result) → retry_commandをresultに付与
    #   2. archive_hook(result) → アーカイブ書き込み（cachedの場合はスキップ）
    #   3. formatter.on_result(ctx, result) → JSONL streamingなど（cachedでも呼ばれる）
    # 上記1+2をcomposed_hookにまとめ、3はrun_commands_with_cliのon_result引数として渡す。
    # これによりcachedの場合でもformatter.on_resultが呼ばれる（CLIとMCPで共通）。
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

    # run
    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    fail_fast = bool(getattr(args, "fail_fast", False))
    try:
        if use_ui:
            results, returncode = pyfltr.output.ui.run_commands_with_ui(
                commands,
                args,
                base_ctx,
                archive_hook=composed_hook,
                slot_wait_hook=slot_wait_hook,
                on_result=_on_result_callback,
                fail_fast=fail_fast,
                only_failed_targets=only_failed_targets,
            )
            # TUI経路では常にinclude_details=True（ストリーミングしていないため）。
            ctx = dataclasses.replace(ctx, stream=False, include_details=True)
        else:
            # 非TUIモード: 既定はバッファリング （最後にまとめて出力）、`--stream` で従来の即時出力。
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
    return results, returncode, ctx


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
) -> PipelineOutcome:
    """完了判定・出力・アーカイブ保存を行う。一時資源の解放は呼出元の`finally`が行う。"""
    # returncodeを先に確定させる （render_resultsに渡してJSONL summary.exitに埋めるため）
    # TUIのCtrl+C協調停止は `run_commands_with_ui` から130 （SIGINT慣例） を返す。
    # この場合は `calculate_returncode` で上書きせず、そのまま採用する。
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)

    # 直接指定されたパスが部分的に不在で、かつ実行された全コマンドがskippedの場合は
    # 意図しない呼び出し（指定ファイルが見つからずほぼ何も実行されなかった）の可能性が高いため
    # exit 1で検知する。全件不在は手前の早期exit経路で処理済みのためここでは部分不在のみが対象。
    # resultsが空（有効化コマンド自体がゼロ）の場合は対象外。
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

    pyfltr.cli.run_storage.finalize_archive(
        archive_store, ctx.run_id, returncode, ctx.commands, ctx.all_files, archive_required=archive_required
    )

    # pre-commit経由かつformatter自動修正発生時のMM状態ガイダンスを必要に応じて出力する。
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
    """`--commands`の明示指定と、蓄積された直接指定の不在・全除外と合わせて完了判定を導出する。"""
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
    """pre-commit経由かつformatter修正発生時にMM状態ガイダンスをstderrへ出力する。

    `git commit` から起動されたpre-commit経由でpyfltrがformatterを実行すると、
    修正結果がワークツリーには書き込まれる一方でindexには反映されない （MM状態）。
    この場合に限り `git add` を促すメッセージを人間向け （日本語） で出力する。

    構造化stdoutモード （`jsonl` / `sarif` / `code-quality` をstdoutに出力する） では、
    stderrにtextが既に出力されているため重複を避ける意味でも抑止する。`github-annotations`
    はtextと同じレイアウトをstdoutに出力するため抑止不要。
    `quiet=True`のときはエージェント経路のstderrノイズ削減目的でメッセージ自体を抑止する。
    """
    if quiet or structured_stdout:
        return
    if not any(result.formatted for result in results):
        return
    if not pyfltr.command.precommit_guidance.is_invoked_from_git_commit():
        return
    print(_PRECOMMIT_MM_MESSAGE, file=sys.stderr)


def calculate_returncode(results: list[pyfltr.command.core_.CommandResult], exit_zero_even_if_formatted: bool) -> int:
    """終了コードを計算。"""
    status = pyfltr.command.core_.overall_status(results)
    return int(status == "FAILED" or (status == "FORMATTED" and not exit_zero_even_if_formatted))


_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(pyfltr.output.formatters.FORMATTERS.keys())


def load_run_config(config_dir: pathlib.Path | None = None, *, archive_required: bool = False) -> pyfltr.config.model.Config:
    """実行起点の設定を読み、応答がrun_idを要する入口ではアーカイブを有効化する。"""
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
    """CLIとMCPに共通の設定読込・上書き・エイリアス展開・コマンド検証を行う。

    CLIはカスタム引数の登録に使った設定を渡し、同じ設定を再読込しない。
    公開入力の解析と、ValueErrorを各入口のエラーへ変換する処理は呼び出し側が担う。
    """
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
    """実行系サブコマンド向けに出力形式を解決する。

    `run-for-agent`のみサブコマンド既定値`"jsonl"`を渡し、`AGENT_INDICATOR_ENVS`のいずれかが
    検出された場合は実行系全体で`jsonl`既定を採用する。
    `PYFLTR_OUTPUT_FORMAT=text`での変更は`cli/output_format.py`側で扱う。

    エージェント検出環境下では`--quiet`既定値も`cli/parser.py`側で有効化されるため、
    `run`と`run-for-agent`は等価に振る舞う。
    """
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
    """run()の内部実装（実行系サブコマンド向け）。

    `reparse_fn` はカスタムコマンド用にparserを再構築する関数。
    `cli/main.py`側で`cli/parser`への参照を保持することで、
    `cli/pipeline`→`cli/parser`の直接依存（循環importの原因）を避ける。
    """
    # 同一プロセス内でrun() が複数回呼ばれるケースに備えて警告蓄積を初期化する。
    pyfltr.warnings_.clear()

    # --ciオプションの処理
    if args.ci:
        args.shuffle = False
        args.no_ui = True

    # --from-runは--only-failedとの併用が必須。
    # 単独利用を許可しない理由: --from-run単独ではdiagnostic参照は行われず、
    # 「再実行対象を指定runの失敗ツールに限定する」という本来の意味を持たない。
    # argparse段階で拒否することでユーザーに正しい併用形を即座に提示できる。
    if getattr(args, "from_run", None) is not None and not getattr(args, "only_failed", False):
        parser.error("argument --from-run: requires --only-failed")

    # --uiと--no-uiの競合チェック
    if args.ui and args.no_ui:
        parser.error("--ui と --no-ui は同時に指定できません。どちらか一方を指定してください。")

    # --version （実行系サブコマンド下でも許容）。単独指定と同じく標準出力へ書く。
    if args.version:
        print(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    resolution = _resolve_output_format(parser, args)
    output_format, format_source = resolution.format, resolution.source
    output_file: pathlib.Path | None = args.output_file

    # pyproject.toml
    try:
        config = load_run_config(args.work_dir)
    except (ValueError, OSError) as e:
        logger.error(f"設定エラー: {pyfltr.config.config.format_config_error(e, retry='元の実行コマンド')}")
        return 1

    args.output_format = output_format
    args.format_source = format_source
    args.output_file = output_file
    work_dir = args.work_dir

    # カスタムコマンド用のCLI引数を動的追加して再パース。
    # reparse_fnはcli/main.pyが渡すコールバックで、cli/parserへの直接依存を持たずに済む。
    custom_commands = [name for name, info in config.commands.items() if not info.builtin]
    if custom_commands and reparse_fn is not None:
        parser, args = reparse_fn(custom_commands)
        # 再パースで各種属性が初期化されるため、確定済みの値を再適用する。
        args.output_format = output_format
        args.format_source = format_source
        args.output_file = output_file
        args.work_dir = work_dir
        if getattr(args, "no_fix", False):
            args.include_fix_stage = False
        if args.ci:
            args.shuffle = False
            args.no_ui = True

    # --work-dir指定時、再パースで上書きされたtargetsを絶対パスで復元
    if resolved_targets is not None:
        args.targets = resolved_targets

    # CLIオプションでconfigを上書き（サブプロジェクト別configにも同一に再適用するため共通ヘルパーへ集約）
    options = pyfltr.run_options.RunOptions.from_values(vars(args))

    # --commands未指定時はカスタムコマンドを含む全登録コマンドを対象にする。
    # argparseのデフォルト評価時点ではpyproject.tomlを読み込んでいないため、
    # ビルトインのみのdefaultを返すとcustom-commandsが常にスキップされる。
    # load_config後に実体を決定することで、ユーザーが登録したcustom-commands
    # （例: svelte-check） も `run` / `ci` サブコマンドのデフォルト動作で実行されるようにする。
    # `--commands` は `action="append"` によりリストで渡るため、各要素を
    # カンマ区切りで再分割して平坦化する。重複は先出を優先して除去する。
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
