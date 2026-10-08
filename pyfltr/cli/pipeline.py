"""パイプライン実行と結果整形。

非TUI経路でのコマンド実行（`run_commands_with_cli`）と、
パイプライン全体を駆動する`run_impl`・`run_pipeline`・
`calculate_returncode`を担う。
text整形描画（`render_results` / `write_log`）は`output/render.py`に分離している。
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib.metadata
import logging
import os
import pathlib
import shlex
import subprocess
import sys
import threading
import time
import typing

import pyfltr.cli.command_selection
import pyfltr.cli.output_format
import pyfltr.cli.overrides
import pyfltr.cli.subproject_config
import pyfltr.command.completion
import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.executor
import pyfltr.command.only_failed
import pyfltr.command.precommit_guidance
import pyfltr.command.process
import pyfltr.command.stage_runner
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection
import pyfltr.config.validation
import pyfltr.output.formatters
import pyfltr.output.jsonl
import pyfltr.output.logging_
import pyfltr.output.render
import pyfltr.output.ui
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.state.retry
import pyfltr.tools
import pyfltr.warnings_

logger = logging.getLogger(__name__)

# text_logger / structured_logger / text_output_lock は output/logging_.py から参照する。
text_logger = pyfltr.output.logging_.text_logger
structured_logger = pyfltr.output.logging_.structured_logger
lock = pyfltr.output.logging_.text_output_lock


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


# heartbeat監視の発火しきい値（秒）。
# パイプライン全体の「最後のJSONL出力からの経過時間」がこの値を超えると、
# 実行中コマンドそれぞれに対して `status:"running"` レコードを発行する。
# 設定キー化は複雑度を増やすだけで利用頻度が低いため固定値とする。
_HEARTBEAT_THRESHOLD_SECONDS: float = 30.0
"""heartbeat発火しきい値（最後のJSONL出力からの無音時間、秒）。"""

_HEARTBEAT_TICK_INTERVAL: float = 5.0
"""heartbeat監視ループの判定間隔（秒）。

しきい値より細かく刻むことで、しきい値超過の検知遅れを最大 `_HEARTBEAT_TICK_INTERVAL` 秒に抑える。
発火自体は最終出力からの経過時間で判定するため、本値は「監視解像度」の調整値。
"""


class HeartbeatMonitor:
    """パイプライン全体のheartbeat監視。

    別スレッドで一定間隔ループし、`pyfltr.output.jsonl.get_last_jsonl_output_time()` から
    最終JSONL出力時刻を取得して経過時間を判定する。
    発火条件は「最後のJSONL出力から既定 `_HEARTBEAT_THRESHOLD_SECONDS` 秒経過した場合」のみ。
    subprocess側のstdout出力の有無は判定材料に含めない（LLMから観測できるのはpyfltr側のJSONL
    出力のみで、子プロセスの静粛さは観測対象外のため）。
    しきい値超過時は実行中コマンド集合（`{command_name -> start_time}`）から各commandへ
    `status:"running"` レコードを発行する。複数コマンドが同時にハングしている場合も
    各コマンドの状態を識別できるよう、実行中の全コマンドそれぞれに発行する設計。
    完了時の最終レコード（`status:"failed"` / `"succeeded"` 等）が必ず後続することを保証する。
    text_loggerにも発火を残し、人間向け表示でも進捗が確認できるようにする。

    `start()` / `stop()` を `run_pipeline` の入口・出口で呼ぶ。
    `on_command_start(command)` / `on_command_end(command)` は subprocess の開始・終了に
    フックして登録・削除する。並列実行下のアクセスは `_lock` で保護する。
    """

    def __init__(
        self,
        *,
        threshold: float = _HEARTBEAT_THRESHOLD_SECONDS,
        tick_interval: float = _HEARTBEAT_TICK_INTERVAL,
        emit_running: typing.Callable[[str, float], None] | None = None,
        emit_text: typing.Callable[[str], None] | None = None,
        get_last_output_time: typing.Callable[[], float | None] | None = None,
        set_last_output_time: typing.Callable[[float], None] | None = None,
    ) -> None:
        self._threshold = threshold
        self._tick_interval = tick_interval
        self._emit_running = emit_running or pyfltr.output.jsonl.write_jsonl_running_event
        self._emit_text = emit_text or _heartbeat_text_emit
        self._get_last_output_time = get_last_output_time or pyfltr.output.jsonl.get_last_jsonl_output_time
        self._set_last_output_time = set_last_output_time or pyfltr.output.jsonl.set_last_jsonl_output_time
        self._lock = threading.Lock()
        self._running: dict[str, float] = {}
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """監視スレッドを起動する。`stop()` 呼び出しまで非daemon相当でループする。

        起動時点を初期最終出力時刻として常時上書きする。
        モジュールグローバルな`_last_jsonl_output_time`は同一プロセスで`run_pipeline`が
        複数回呼ばれるMCP経路で前回値が残存し得るため、毎回起動時に明示的に上書きする
        （未初期化判定では2回目以降の起動で前回値がそのまま使われ、
        起動直後にしきい値超過とみなされる誤発火を招く）。
        """
        if self._thread is not None:
            return
        self._set_last_output_time(time.monotonic())
        self._stop_event.clear()
        thread = threading.Thread(target=self._loop, name="pyfltr-heartbeat", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """監視スレッドを停止する。"""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=self._tick_interval * 2)
        self._thread = None

    def on_command_start(self, command: str) -> None:
        """実行中コマンド集合に追加する。"""
        with self._lock:
            self._running[command] = time.monotonic()

    def on_command_end(self, command: str) -> None:
        """実行中コマンド集合から削除する。"""
        with self._lock:
            self._running.pop(command, None)

    def _snapshot_running(self) -> list[tuple[str, float]]:
        """現時点の実行中コマンドのリスト（command, start_time）を返す。"""
        with self._lock:
            return list(self._running.items())

    def _loop(self) -> None:
        """監視ループ本体。`_tick_interval` 毎にしきい値判定を行う。"""
        while not self._stop_event.wait(self._tick_interval):
            self.tick(time.monotonic())

    def tick(self, now: float) -> None:
        """1回分の判定。しきい値超過なら実行中各コマンドへrunningイベントを発行する。

        テストから時刻依存の発火検証を行うためpublic APIとして公開する。
        通常運用では`_loop`から自動的に呼ばれる。
        """
        last_output = self._get_last_output_time()
        if last_output is None:
            return
        silence = now - last_output
        if silence <= self._threshold:
            return
        running = self._snapshot_running()
        if not running:
            # 実行中コマンドが無ければheartbeat発行しても意味が無いため抑止する。
            # ただし無音時間カウンタは更新して連続ティックでの再判定を抑える。
            self._set_last_output_time(now)
            return
        for command, started in running:
            elapsed = now - started
            self._emit_text(f"{command} running for {elapsed:.0f}s (no JSONL output for {silence:.0f}s)")
            self._emit_running(command, elapsed)
        # heartbeat発火後、`_emit_running`内の `_emit_structured` で `last_jsonl_output_time` が
        # 自動的に更新されるため、明示更新は不要。次の判定はそこから再びしきい値経過後に発火する。


def _heartbeat_text_emit(message: str) -> None:
    """heartbeat発火時のtext_logger経由warning出力。

    text_loggerはoutput_formatごとに出力先（stdout/stderr）が切り替わるため、
    本関数はその差分を吸収する単純なラッパー。
    """
    with lock:
        text_logger.warning(message)


_ARCHIVE_FAILURE_HINT = (
    "キャッシュディレクトリの書き込み権限と空き容量を確認してください。"
    "実行アーカイブが不要なら `--no-archive` を指定すると警告を抑止できます。"
)


def _make_archive_hook(
    archive_store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
) -> typing.Callable[[pyfltr.command.core_.CommandResult], None]:
    """アーカイブ書き込みフックを生成する。

    書き込みに成功した場合のみ`result.archived = True`を設定し、
    smart truncationの可否判定に使う。失敗時は警告を発行して処理を続行する。
    """

    def _hook(result: pyfltr.command.core_.CommandResult) -> None:
        try:
            archive_store.write_tool_result(run_id, result)
        except OSError as e:
            # ハンドラ内でwarningを通知してもsummary末尾にまとまる。
            pyfltr.warnings_.emit_warning(
                source="archive",
                message=(
                    f"{result.command} の結果を実行アーカイブへ書き込めませんでした: {e}。"
                    "このコマンドの結果は `show-run` と `--only-failed` から参照できません"
                ),
                hint=_ARCHIVE_FAILURE_HINT,
            )
            return
        # 書き込み成功時のみarchived=Trueに更新。smart truncationの可否判定に使う。
        result.archived = True

    return _hook


def _make_attach_retry_command(
    *,
    retry_args_template: list[str],
    launcher_prefix: list[str],
    original_cwd: str,
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
            original_cwd=original_cwd,
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
    fail_fast: bool = False,
    only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None = None,
    heartbeat: HeartbeatMonitor | None = None,
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

    `archive_hook`が指定されている場合、各コマンド完了時に実行アーカイブへ書き込む。
    fixステージの結果はsummaryに含めないが、アーカイブには通常ステージ以外も含めて
    全実行を保存するためfixステージからも`archive_hook`を呼び出す。

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

    return pyfltr.command.stage_runner.run_stages(
        commands,
        base_ctx,
        execute,
        include_fix_stage=include_fix_stage,
        fail_fast=fail_fast,
        callbacks=pyfltr.command.stage_runner.StageCallbacks(archive=archive_hook, on_result=on_result),
    )


def _run_one_command(
    command: str,
    args: pyfltr.run_options.RunOptions,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    per_command_log: bool,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.command.only_failed.ToolTargets | None = None,
    heartbeat: HeartbeatMonitor | None = None,
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
    original_cwd: str | None = None,
    original_sys_args: list[str] | None = None,
    force_text_on_stderr: bool = False,
    jsonl_warnings_reach_consumer: bool = True,
) -> PipelineOutcome:
    """出力形式に応じた警告配送スコープを設定してパイプラインを実行する。"""
    if (args.output_format or "text") == "jsonl":
        with pyfltr.warnings_.defer_stderr():
            return _run_pipeline(
                args,
                commands,
                config,
                start_cwd=start_cwd,
                original_cwd=original_cwd,
                original_sys_args=original_sys_args,
                force_text_on_stderr=force_text_on_stderr,
                jsonl_warnings_reach_consumer=jsonl_warnings_reach_consumer,
            )
    return _run_pipeline(
        args,
        commands,
        config,
        start_cwd=start_cwd,
        original_cwd=original_cwd,
        original_sys_args=original_sys_args,
        force_text_on_stderr=force_text_on_stderr,
        jsonl_warnings_reach_consumer=jsonl_warnings_reach_consumer,
    )


def _run_pipeline(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    config: pyfltr.config.model.Config,
    *,
    start_cwd: pathlib.Path | None = None,
    original_cwd: str | None = None,
    original_sys_args: list[str] | None = None,
    force_text_on_stderr: bool = False,
    jsonl_warnings_reach_consumer: bool = True,
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
        返す。MCP経路はこの `run_id is None` を「実行スキップ」として識別する。
        `completion` はearly exitを含む全経路で確定し、`formatter.on_finish` より前に
        出力文脈へ渡してJSONL `summary` と同じ値にする。

    戻り値で`run_id`を返すのはMCP経路がrun_idを確実に取得するため。
    代替案としてMCP側で `ArchiveStore.list_runs(limit=1)` を引く案も検討
    したが、同一ユーザーキャッシュを参照する並行プロセスがあると別runの
    `run_id` を誤って拾うリスクがあるため戻り値経由とした。

    heartbeat監視 (`HeartbeatMonitor`) は出力形式が `jsonl` のときに限定して起動する。
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
    prepared = _prepare_execution_targets(args, commands, config, start_cwd_path, formatter, early_ctx)
    if isinstance(prepared, PipelineOutcome):
        return prepared

    effective_cwd = original_cwd if original_cwd is not None else os.getcwd()
    effective_sys_args = list(original_sys_args) if original_sys_args is not None else list(sys.argv[1:])
    launcher_prefix = pyfltr.state.retry.detect_launcher_prefix()
    retry_args_template = pyfltr.state.retry.build_retry_args_template(effective_sys_args)
    archive_store, run_id = _initialize_archive(args, config, prepared.commands, prepared.all_files, start_cwd_path)
    _log_runtime(start_cwd_path, run_id, launcher_prefix)
    cache_store = _initialize_cache(args, config)
    archive_hook = _make_archive_hook(archive_store, run_id) if archive_store is not None and run_id is not None else None
    attach_retry_command = _make_attach_retry_command(
        retry_args_template=retry_args_template,
        launcher_prefix=launcher_prefix,
        original_cwd=effective_cwd,
    )
    base_ctx = _prepare_execution_context(config, prepared, start_cwd_path, cache_store, run_id)
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
        attach_retry_command=attach_retry_command,
        only_failed_targets=prepared.only_failed_targets,
    )
    return _finish_pipeline(args, base_ctx, formatter, ctx, results, returncode, archive_store, prepared.unmet)


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
    prepared: PreparedTargets,
    start_cwd_path: pathlib.Path,
    cache_store: pyfltr.state.cache.CacheStore | None,
    run_id: str | None,
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
    )
    if "pytest" in prepared.commands:
        base_ctx.pytest_base = _build_pytest_base(base_ctx, fast_selected=prepared.fast_selected)

    return base_ctx


def _prepare_run_output(
    args: pyfltr.run_options.RunOptions,
    formatter: pyfltr.output.formatters.OutputFormatter,
    early_ctx: pyfltr.output.formatters.RunOutputContext,
    prepared: PreparedTargets,
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
    attach_retry_command: typing.Callable[[pyfltr.command.core_.CommandResult], None],
    only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None,
) -> tuple[list[pyfltr.command.core_.CommandResult], int, pyfltr.output.formatters.RunOutputContext]:
    """CLI/TUIの実行と結果フックを接続し、heartbeatの生存期間を管理する。"""
    use_ui = not args.no_ui and (args.ui or pyfltr.output.ui.can_use_ui())
    # heartbeat監視の起動。
    # `jsonl`形式のときに限定して起動する。
    # `sarif`・`code-quality`はbufferingformatter（`on_finish`で単一JSONドキュメントを一括出力）のため、
    # 途中で`status:"running"`レコードを混入させると最終的な出力（SARIF 2.1.0オブジェクト・
    # Code Climate JSON配列）が不正な形式になる。
    # `text`等のJSONLレコードが流れない出力形式ではheartbeatの観測対象が成立しない。
    # TUI経路はUIに進捗表示があるため別途heartbeat不要。
    heartbeat: HeartbeatMonitor | None = None
    if args.output_format == "jsonl" and not use_ui and ctx.jsonl_warnings_reach_consumer:
        heartbeat = HeartbeatMonitor()
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
) -> PipelineOutcome:
    """完了判定・出力・アーカイブ保存・一時資源の解放を行う。"""
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

    _finalize_archive(archive_store, ctx.run_id, returncode, ctx.commands, ctx.all_files)

    # pre-commit経由かつformatter自動修正発生時のMM状態ガイダンスを必要に応じて出力する。
    _maybe_emit_precommit_guidance(results, structured_stdout=ctx.structured_stdout, quiet=ctx.quiet)

    base_ctx.cleanup()

    return PipelineOutcome(returncode, ctx.run_id, completion, tuple(results))


@dataclasses.dataclass
class PreparedTargets:
    """設定と対象展開を確定した実行準備の結果。"""

    commands: list[str]
    all_files: list[pathlib.Path]
    subprojects: list[pyfltr.command.subprojects.Subproject]
    subproject_files: dict[pathlib.Path, list[pathlib.Path]]
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config]
    external_files: list[pathlib.Path]
    only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None
    fast_selected: bool
    unmet: list[str]


def _prepare_execution_targets(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    config: pyfltr.config.model.Config,
    start_cwd_path: pathlib.Path,
    formatter: pyfltr.output.formatters.OutputFormatter,
    early_run_ctx: pyfltr.output.formatters.RunOutputContext,
) -> PreparedTargets | PipelineOutcome:
    """対象展開、差分・失敗対象の限定とコマンド選択を行う。"""
    subprojects = pyfltr.command.subprojects.discover_subprojects(start_cwd_path, config)
    if len(subprojects) < 2:
        # モノレポモード非適用: subprojects を空集合として扱う（dispatcher 側で単一経路）。
        subprojects = []

    # 対象ファイルを一括展開（ディレクトリ走査・exclude・gitignoreフィルタリングを1回だけ実行）
    # TUI起動前に実行することで、除外警告がログに表示される。
    # `start_cwd` を明示してプロセスグローバルな cwd 干渉を避ける。
    # サブプロジェクトの最終的な所属判定は `classify_files_by_subproject` の最深一致で行うため、
    # `exclude_subdirs` は渡さず全ファイルを一度収集する。
    all_files = pyfltr.command.targets.expand_all_files(
        args.targets,
        config,
        start_cwd=start_cwd_path,
    )

    # ユーザー指定パスが全て非存在の場合は、各ツールが個別に「ファイルが見つからない」エラーを
    # 多重に出力する前段で打ち切り、非ゼロ終了する。warning自体は`expand_all_files`内で発行済み。
    # 部分一致（一部のみ不在）は処理継続、対象未指定（カレント走査）も対象外として扱う。
    if args.targets and len(pyfltr.warnings_.filtered_direct_files(reason="missing")) == len(args.targets):
        completion = _evaluate_completion([], args, config, files_reached=0, unmet_commands=[])
        early_run_ctx = dataclasses.replace(early_run_ctx, completion=completion)
        formatter.on_start(early_run_ctx)
        formatter.on_finish(early_run_ctx, [], 1, pyfltr.warnings_.collected_warnings())
        return PipelineOutcome(1, None, completion)

    # --changed-since指定時はgit差分ファイルとの交差でフィルタリングする。
    # --only-failedよりも先に適用し、以後のフィルタはフィルタリング済みリストを受け取る。
    changed_since_ref: str | None = getattr(args, "changed_since", None)
    if changed_since_ref is not None:
        all_files = pyfltr.command.targets.filter_by_changed_since(all_files, changed_since_ref, cwd=start_cwd_path)

    # モノレポ用のサブプロジェクト別 config を準備する。
    # `subproject_aware=True` ツールが各サブプロジェクト cwd で実行する際に参照する。
    # config解決（`pyproject.toml`不在時の最近接祖先継承を含む）は
    # `pyfltr.cli.subproject_config.resolve_subproject_configs` に集約する。
    # `--only-failed`の候補（`pytest-always-targets`）と実行対象コマンドの確定（和集合判定）より前に構築する。
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config] = {}
    if subprojects:
        subproject_configs = pyfltr.cli.subproject_config.resolve_subproject_configs(subprojects, config, args)

    # --only-failed指定時は直前runからツール別の失敗ファイル集合を構築する。
    # archive / cache初期化より前に実行し、早期終了の場合はそれらの副作用を発生させない。
    commands, only_failed_targets, only_failed_exit_early = pyfltr.state.only_failed.apply_filter(
        args,
        commands,
        all_files,
        from_run=getattr(args, "from_run", None),
        extra_candidates=_only_failed_extra_candidates(args, commands, config, subproject_configs, start_cwd_path),
    )
    if only_failed_exit_early:
        # スキップの理由は`apply_filter`が警告として発行済み。JSONLでもheader・warning・summaryを
        # 出力し、何も出力せずに終了コード0で終わる状態（理由が消費主体へ届かない）を避ける。
        completion = _evaluate_completion([], args, config, files_reached=0, unmet_commands=[])
        early_run_ctx = dataclasses.replace(early_run_ctx, completion=completion)
        formatter.on_start(early_run_ctx)
        formatter.on_finish(early_run_ctx, [], 0, pyfltr.warnings_.collected_warnings())
        return PipelineOutcome(0, None, completion)

    # モノレポ用のサブプロジェクト分類を準備する。
    subproject_files: dict[pathlib.Path, list[pathlib.Path]] = {}
    external_files: list[pathlib.Path] = []
    if subprojects:
        subproject_files, external_files = pyfltr.command.subprojects.classify_files_by_subproject(
            all_files, subprojects, start_cwd_path
        )

    # 実行対象として有効化されていないコマンドはパイプラインから除外する。
    # 単一プロジェクトでは起点 config のON/OFF（`config.values.get(cmd) is True`）で判定する。
    # モノレポでは `subproject_aware=True` ツールに限り「起点またはいずれかのサブプロジェクトで有効」
    # の和集合で対象に含める（親OFF・子ONを子でのみ実行できるようにするため）。
    # `subproject_aware=False`（リポジトリ単位ツール）と `subproject_aware` 判定自体は起点 config で固定する。
    fast_selected = _is_fast_selection(args, config)
    if fast_selected and not getattr(args, "only_failed", False):
        commands = _add_subproject_fast_pytest(commands, args, config, subproject_configs)
    requested_commands = list(commands)
    commands = [c for c in commands if pyfltr.config.selection.is_command_enabled_anywhere(c, config, subproject_configs)]
    # 設定ファイル欠落の警告は確定した実行対象だけを判定する（未選択コマンドの警告を返さない）。
    pyfltr.config.validation.warn_config_files(config, start_cwd_path, commands)
    unmet: list[str] = []
    if getattr(args, "commands", None) is not None:
        unmet = pyfltr.cli.command_selection.compute_unmet_commands(
            pyfltr.cli.command_selection.flatten_commands_arg(args.commands, config),
            requested_commands,
            commands,
            config,
        )
        if unmet:
            pyfltr.warnings_.emit_warning(
                source="commands",
                message="--commandsで指定されたが有効化されていないため未実行のコマンドがあります: " + ", ".join(unmet),
                hint="--enable="
                + ",".join(unmet)
                + " または pyproject.toml [tool.pyfltr] で指定したコマンドを true に設定して有効化してください。",
            )

    return PreparedTargets(
        commands=commands,
        all_files=all_files,
        subprojects=subprojects,
        subproject_files=subproject_files,
        subproject_configs=subproject_configs,
        external_files=external_files,
        only_failed_targets=only_failed_targets,
        fast_selected=fast_selected,
        unmet=unmet,
    )


def _initialize_archive(
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    commands: list[str],
    all_files: list[pathlib.Path],
    start_cwd_path: pathlib.Path,
) -> tuple[pyfltr.state.archive.ArchiveStore | None, str | None]:
    """実行アーカイブを初期化し、失敗時は警告とともに無効化する。"""
    # 実行アーカイブの初期化 （既定で有効）。
    # `--no-archive` または `archive = false` で無効化できる。クリーンアップ失敗や
    # 書き込み失敗はパイプライン本体を止めないようwarningsへ転送する。
    archive_enabled = bool(config.values.get("archive", True)) and not getattr(args, "no_archive", False)
    archive_store: pyfltr.state.archive.ArchiveStore | None = None
    run_id: str | None = None
    if archive_enabled:
        try:
            archive_store = pyfltr.state.archive.ArchiveStore()
            run_id = archive_store.start_run(commands=commands, files=len(all_files), cwd=str(start_cwd_path))
            removed = archive_store.cleanup(pyfltr.state.archive.policy_from_config(config))
            if removed:
                logger.debug("archive: 自動削除で %d 件の古い run を削除", len(removed))
        except OSError as e:
            pyfltr.warnings_.emit_warning(
                source="archive",
                message=(
                    f"実行アーカイブを初期化できないため、アーカイブ無しで続行しました: {e}。"
                    "この実行にはrun_idが付かず、`show-run` と次回の `--only-failed` から参照できません"
                ),
                hint=_ARCHIVE_FAILURE_HINT,
            )
            archive_store = None
            run_id = None

    return archive_store, run_id


def _initialize_cache(
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
) -> pyfltr.state.cache.CacheStore | None:
    """ファイルキャッシュを初期化し、失敗時は警告とともに無効化する。"""
    # ファイルhashキャッシュの初期化 （既定で有効）。
    # `--no-cache` または `cache = false` で無効化できる。期間超過エントリの削除失敗や
    # 書き込み失敗はパイプライン本体を止めないためwarningsへ転送する。
    cache_enabled = bool(config.values.get("cache", True)) and not getattr(args, "no_cache", False)
    cache_store: pyfltr.state.cache.CacheStore | None = None
    if cache_enabled:
        try:
            cache_store = pyfltr.state.cache.CacheStore()
            cache_removed = cache_store.cleanup(pyfltr.state.cache.cache_policy_from_config(config))
            if cache_removed:
                logger.debug("cache: 期間超過で %d 件のエントリを削除", len(cache_removed))
        except OSError as e:
            pyfltr.warnings_.emit_warning(
                source="cache",
                message=(
                    f"ファイル hash キャッシュを初期化できないため、キャッシュ無しで続行しました: {e}。"
                    "検査結果は変わりませんが、変更の無いファイルも再検査します"
                ),
                hint=(
                    "キャッシュディレクトリの書き込み権限と空き容量を確認してください。"
                    "キャッシュが不要なら `--no-cache` を指定すると警告を抑止できます。"
                ),
            )
            cache_store = None

    return cache_store


def _finalize_archive(
    archive_store: pyfltr.state.archive.ArchiveStore | None,
    run_id: str | None,
    returncode: int,
    commands: list[str],
    files: int,
) -> None:
    """終了コードと完了時刻を保存する。保存失敗は検査結果を変えない。"""
    # アーカイブ終端: meta.jsonにexit_code / finished_atを書き込む。
    if archive_store is not None and run_id is not None:
        try:
            archive_store.finalize_run(run_id, exit_code=returncode, commands=commands, files=files)
        except OSError as e:
            pyfltr.warnings_.emit_warning(
                source="archive",
                message=(
                    f"実行アーカイブの meta.json を更新できませんでした: {e}。"
                    "`list-runs` にこの実行の終了コードと完了時刻が表示されません"
                ),
                hint=_ARCHIVE_FAILURE_HINT,
            )


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


def _is_fast_selection(args: pyfltr.run_options.RunOptions, config: pyfltr.config.model.Config) -> bool:
    """fastサブコマンド、または`--commands`にfastエイリアスを含む実行かを返す。"""
    if getattr(args, "subcommand", None) == "fast":
        return True
    raw_commands = getattr(args, "commands", None)
    if raw_commands is None:
        return False
    return "fast" in pyfltr.cli.command_selection.flatten_commands_arg(raw_commands, config)


def _add_subproject_fast_pytest(
    commands: list[str],
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config],
) -> list[str]:
    """起点設定がpytestをfastへ含めない場合に、サブプロジェクトの`pytest-fast-targets`からpytestを加える。

    fastエイリアスは起点設定から展開されるため、起点に指定が無くサブプロジェクトだけが
    `pytest-fast-targets`を持つモノレポでは、展開結果にpytestが入らない。
    fastエイリアスを指定した実行に限り、pytestを有効にして非空の指定を持つサブプロジェクトがあれば加える。
    """
    if "pytest" in commands or "fast" not in pyfltr.cli.command_selection.flatten_commands_arg(args.commands, config):
        return commands
    if not any(
        sub_config.values.get("pytest") is True and pyfltr.config.selection.pytest_fast_target_globs(sub_config.values)
        for sub_config in subproject_configs.values()
    ):
        return commands
    return sorted([*commands, "pytest"], key=lambda name: pyfltr.tools.command_index(config.command_names, name))


def _only_failed_extra_candidates(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    config: pyfltr.config.model.Config,
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config],
    start_cwd_path: pathlib.Path,
) -> dict[str, list[pathlib.Path]] | None:
    """`--only-failed`の交差の候補へ、`pytest-always-targets`に一致するプロジェクト全域のファイルを加える。

    位置引数・差分指定に依らずpytestの対象へ加わるテストは、前回失敗していれば再実行の対象に残す。
    """
    if not getattr(args, "only_failed", False) or "pytest" not in commands:
        return None
    globs = [
        glob
        for values in [config.values, *(sub.values for sub in subproject_configs.values())]
        for glob in pyfltr.config.selection.pytest_always_target_globs(values)
    ]
    if not globs:
        return None
    full_files = pyfltr.command.targets.expand_all_files([], config, start_cwd=start_cwd_path)
    return {"pytest": pyfltr.command.targets.filter_by_globs(full_files, globs)}


def _build_pytest_base(
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    fast_selected: bool,
) -> pyfltr.command.core_.ExecutionBaseContext | None:
    """pytestだけへ使う実行基盤を構築する。

    fast選択時に`pytest-fast-targets`を持つ設定（起点または各サブプロジェクト）では、位置引数・差分指定に
    依らずプロジェクト全域を走査したファイル集合を母集合とする。
    `pytest-always-targets`を持つ設定では、母集合へプロジェクト全域から同設定のglobに一致したファイルを加える。
    dispatcherはこの追加分を通常の対象globに依らずpytestの対象へ含めるため、通常の対象との和集合になる。
    指定の無い設定は従来の集合を保つ。いずれの設定も指定を持たない場合は`None`を返し、
    pytestも通常の実行基盤で動かす。
    """
    config = base_ctx.config

    def _fast_globs(values: dict[str, typing.Any]) -> list[str]:
        return pyfltr.config.selection.pytest_fast_target_globs(values) if fast_selected else []

    def _always_globs(values: dict[str, typing.Any]) -> list[str]:
        return pyfltr.config.selection.pytest_always_target_globs(values)

    sub_values = {sub.cwd: base_ctx.subproject_configs.get(sub.cwd, config).values for sub in base_ctx.subprojects}
    root_uses_full = bool(_fast_globs(config.values) or _always_globs(config.values))
    subs_using_full = [cwd for cwd, values in sub_values.items() if _fast_globs(values) or _always_globs(values)]
    if not root_uses_full and not subs_using_full:
        return None
    full_files = pyfltr.command.targets.expand_all_files([], config, start_cwd=base_ctx.start_cwd)

    def _merge(current: list[pathlib.Path], full: list[pathlib.Path], values: dict[str, typing.Any]) -> list[pathlib.Path]:
        base_files = full if _fast_globs(values) else current
        always_files = pyfltr.command.targets.filter_by_globs(full, _always_globs(values))
        # 位置引数の絶対パスと全域走査の相対パスが同じ実体を指す場合も1件にする。
        seen = {(base_ctx.start_cwd / f).resolve() for f in base_files}
        return [*base_files, *(f for f in always_files if (base_ctx.start_cwd / f).resolve() not in seen)]

    subproject_files = dict(base_ctx.subproject_files)
    if subs_using_full:
        full_subproject_files, _ = pyfltr.command.subprojects.classify_files_by_subproject(
            full_files, base_ctx.subprojects, base_ctx.start_cwd
        )
        for cwd in subs_using_full:
            subproject_files[cwd] = _merge(
                base_ctx.subproject_files.get(cwd, []), full_subproject_files.get(cwd, []), sub_values[cwd]
            )
    return dataclasses.replace(
        base_ctx,
        all_files=_merge(base_ctx.all_files, full_files, config.values) if root_uses_full else base_ctx.all_files,
        subproject_files=subproject_files,
        pytest_base=None,
        pytest_fast_targets_active=fast_selected,
        pytest_always_targets_active=True,
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
        parser.error("--ui と --no-ui は同時に指定できません。")

    # --version （実行系サブコマンド下でも許容）
    if args.version:
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    resolution = _resolve_output_format(parser, args)
    output_format, format_source = resolution.format, resolution.source
    output_file: pathlib.Path | None = args.output_file

    # pyproject.toml
    try:
        config = load_run_config(args.work_dir)
    except (ValueError, OSError) as e:
        logger.error(f"設定エラー: {e}")
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
        original_cwd=original_cwd,
        original_sys_args=options.retry_arguments(),
    )
    return outcome.exit_code
