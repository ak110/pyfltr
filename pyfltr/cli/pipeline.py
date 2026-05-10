"""パイプライン実行と結果整形。

非TUI経路でのコマンド実行（`run_commands_with_cli`）と、
パイプライン全体を駆動する`run_impl`・`run_pipeline`・
`calculate_returncode`を担う。
text整形描画（`render_results` / `write_log`）は`cli/render.py`に分離している。
"""

from __future__ import annotations

import argparse
import concurrent.futures
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

import pyfltr.cli.output_format
import pyfltr.cli.precommit_guidance
import pyfltr.cli.render
import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.output.formatters
import pyfltr.output.jsonl
import pyfltr.output.ui
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.state.executor
import pyfltr.state.only_failed
import pyfltr.state.retry
import pyfltr.state.stage_runner
import pyfltr.warnings_

logger = logging.getLogger(__name__)

# text_logger / structured_logger / text_output_lock は cli/output_format.py で定義する。
# 本モジュールでは output_format から参照して使う。
text_logger = pyfltr.cli.output_format.text_logger
structured_logger = pyfltr.cli.output_format.structured_logger
lock = pyfltr.cli.output_format.text_output_lock


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
    最終JSONL出力時刻を取得して経過時間を判定する。しきい値超過時は実行中コマンド集合
    （`{command_name -> start_time}`）から各commandへ `status:"running"` レコードを発行する。
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
            pyfltr.warnings_.emit_warning(source="archive", message=f"{result.command} のアーカイブ書き込みに失敗: {e}")
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
    args: argparse.Namespace,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    per_command_log: bool,
    include_fix_stage: bool = False,
    on_result: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    fail_fast: bool = False,
    only_failed_targets: dict[str, pyfltr.state.only_failed.ToolTargets] | None = None,
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

    `fail_fast=True`のとき、いずれかのツール完了時に`has_error=True`を検出した
    時点で未開始のジョブを`future.cancel()`で打ち切り、起動済みサブプロセスに
    `terminate()`を送る。formatterの`formatted`はfailureに含めない。

    `only_failed_targets`が指定された場合、ツール別の失敗ファイル集合を
    `execute_command`へ渡す（`--only-failed`経路で直前runの失敗ファイルのみを
    対象とする）。値が`None`のツールは通常の`all_files`で実行し、`list`の
    ツールはその集合のみを対象にする。
    """
    config = base_ctx.config
    results: list[pyfltr.command.core_.CommandResult] = []
    fixers, formatters, linters_and_testers = pyfltr.state.executor.split_commands_for_execution(
        commands, config, base_ctx.all_files, include_fix_stage=include_fix_stage
    )

    # fixステージ: 同一ファイルへの書き込み競合を避けるため直列実行する。
    # 結果はsummary / jsonlには含めない（後段の通常ステージで同一コマンドが
    # 再度実行して最終状態を報告するため。ruff-formatの2段階と同じ位置づけ）。
    for command in fixers:
        fix_result = _run_one_command(
            command,
            args,
            base_ctx,
            per_command_log=per_command_log,
            fix_stage=True,
            only_failed_targets=pyfltr.command.targets.pick_targets(only_failed_targets, command),
            heartbeat=heartbeat,
        )
        if archive_hook is not None and not fix_result.cached:
            archive_hook(fix_result)
        if fail_fast and fix_result.has_error:
            return _emit_skipped_results(
                results,
                remaining=[*formatters, *linters_and_testers],
                config=config,
                on_result=on_result,
                archive_hook=archive_hook,
            )

    # formattersを順序実行
    for idx, command in enumerate(formatters):
        result = _run_one_command(
            command,
            args,
            base_ctx,
            per_command_log=per_command_log,
            only_failed_targets=pyfltr.command.targets.pick_targets(only_failed_targets, command),
            heartbeat=heartbeat,
        )
        results.append(result)
        if archive_hook is not None and not result.cached:
            archive_hook(result)
        if on_result is not None:
            on_result(result)
        if fail_fast and result.has_error:
            remaining = [*formatters[idx + 1 :], *linters_and_testers]
            return _emit_skipped_results(
                results,
                remaining=remaining,
                config=config,
                on_result=on_result,
                archive_hook=archive_hook,
            )

    # linters/testersを並列実行
    if len(linters_and_testers) > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config["jobs"]) as executor:
            future_to_command = {
                executor.submit(
                    _run_one_command,
                    command,
                    args,
                    base_ctx,
                    per_command_log=per_command_log,
                    only_failed_targets=pyfltr.command.targets.pick_targets(only_failed_targets, command),
                    heartbeat=heartbeat,
                ): command
                for command in linters_and_testers
            }
            aborted = False
            aborted_commands: set[str] = set()
            for future in concurrent.futures.as_completed(future_to_command):
                try:
                    result = future.result()
                except concurrent.futures.CancelledError:
                    aborted_commands.add(future_to_command[future])
                    continue
                results.append(result)
                if archive_hook is not None and not result.cached:
                    archive_hook(result)
                if on_result is not None:
                    on_result(result)
                if fail_fast and not aborted and result.has_error:
                    aborted = True
                    # 未開始ジョブをまとめてキャンセルし、起動済みサブプロセスを中断する。
                    pyfltr.state.stage_runner.cancel_pending_futures(future_to_command, aborted_commands)
                    pyfltr.command.process.terminate_active_processes()
            if aborted_commands:
                for pending_command in aborted_commands:
                    skipped = pyfltr.state.stage_runner.make_skipped_result(pending_command, config)
                    results.append(skipped)
                    if archive_hook is not None:
                        archive_hook(skipped)
                    if on_result is not None:
                        on_result(skipped)

    return results


def _emit_skipped_results(
    results: list[pyfltr.command.core_.CommandResult],
    *,
    remaining: list[str],
    config: pyfltr.config.config.Config,
    on_result: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None,
    archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None,
) -> list[pyfltr.command.core_.CommandResult]:
    """--fail-fast中断時、未実行ツールをskipped扱いで追加する（fix/formatter段から）。"""
    pyfltr.command.process.terminate_active_processes()
    for command in remaining:
        skipped = pyfltr.state.stage_runner.make_skipped_result(command, config)
        results.append(skipped)
        if archive_hook is not None:
            archive_hook(skipped)
        if on_result is not None:
            on_result(skipped)
    return results


def _run_one_command(
    command: str,
    args: argparse.Namespace,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    per_command_log: bool,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.state.only_failed.ToolTargets | None = None,
    heartbeat: HeartbeatMonitor | None = None,
) -> pyfltr.command.core_.CommandResult:
    """1 コマンドの実行。

    `per_command_log=True`ならば完了直後に詳細ログを`write_log()`で出力する。
    それ以外は開始/完了の1行進捗のみ出力する。
    `heartbeat` が指定された場合、subprocess起動・終了に合わせて実行中コマンド集合を更新し、
    パイプラインheartbeatの追跡対象に含める。
    """
    # serial_groupを持つコマンドは同一グループ内で排他実行される（cargo / dotnet等）
    with pyfltr.state.executor.serial_group_lock(base_ctx.config.commands[command].serial_group):
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
            pyfltr.cli.render.write_log(result, use_github_annotations=use_ga)
        else:
            with lock:
                text_logger.info(f"{command}{suffix} 完了 ({result.get_status_text()})")
        return result


def run_pipeline(
    args: argparse.Namespace,
    commands: list[str],
    config: pyfltr.config.config.Config,
    *,
    original_cwd: str | None = None,
    original_sys_args: list[str] | None = None,
    force_text_on_stderr: bool = False,
) -> tuple[int, str | None]:
    """実行パイプライン。

    `force_text_on_stderr=True` を渡すと、人間向けtext整形ログの出力先を
    stdoutではなくstderrに強制する（MCP経路でstdoutをJSON-RPCフレームが
    占有するケース用）。

    Returns:
        `(exit_code, run_id)` のタプル。
        `exit_code` は0 = 成功、1 = 失敗。
        `run_id` は実行アーカイブが有効で採番に成功した場合のULID文字列、
        無効・採番失敗・early exit時は `None`。
        `--only-failed` 指定で「直前runなし」「失敗ツールなし」「対象ファイル
        交差が空」のいずれかに該当する場合はearly exitとして `(0, None)` を
        返す。MCP経路はこの `run_id is None` を「実行スキップ」として識別する。

    タプル戻り値を採用したのはMCP経路がrun_idを確実に取得するため。
    代替案としてMCP側で `ArchiveStore.list_runs(limit=1)` を引く案も検討
    したが、同一ユーザーキャッシュを参照する並行プロセスがあると別runの
    `run_id` を誤って拾うリスクがあるため戻り値経由とした。
    """
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

    # ターミナルをクリア
    if not args.no_clear:
        clear_cmd = ["cmd", "/c", "cls"] if os.name == "nt" else ["clear"]
        subprocess.run(clear_cmd, check=False)

    # 対象ファイルを一括展開（ディレクトリ走査・exclude・gitignoreフィルタリングを1回だけ実行）
    # TUI起動前に実行することで、除外警告がログに表示される
    all_files = pyfltr.command.targets.expand_all_files(args.targets, config)

    # ユーザー指定パスが全て非存在の場合は、各ツールが個別に「ファイルが見つからない」エラーを
    # 多重に出力する前段で打ち切り、非ゼロ終了する。warning自体は`expand_all_files`内で発行済み。
    # 部分一致（一部のみ不在）は処理継続、対象未指定（カレント走査）も対象外として扱う。
    if args.targets and len(pyfltr.warnings_.filtered_direct_files(reason="missing")) == len(args.targets):
        early_run_ctx = pyfltr.output.formatters.RunOutputContext(
            config=config,
            output_file=output_file,
            force_text_on_stderr=force_text_on_stderr,
            commands=[],
            all_files=0,
            format_source=format_source,
        )
        formatter.on_start(early_run_ctx)
        formatter.on_finish(early_run_ctx, [], 1, pyfltr.warnings_.collected_warnings())
        return 1, None

    # --changed-since指定時はgit差分ファイルとの交差でフィルタリングする。
    # --only-failedよりも先に適用し、以後のフィルタはフィルタリング済みリストを受け取る。
    changed_since_ref: str | None = getattr(args, "changed_since", None)
    if changed_since_ref is not None:
        all_files = pyfltr.command.targets.filter_by_changed_since(all_files, changed_since_ref)

    # --only-failed指定時は直前runからツール別の失敗ファイル集合を構築する。
    # archive / cache初期化より前に実行し、早期終了の場合はそれらの副作用を発生させない。
    commands, only_failed_targets, only_failed_exit_early = pyfltr.state.only_failed.apply_filter(
        args, commands, all_files, from_run=getattr(args, "from_run", None)
    )
    if only_failed_exit_early:
        return 0, None

    # 実行対象として有効化されていないコマンドはパイプラインから除外する。
    # split_commands_for_executionと同じ条件 （`config.values.get(cmd) is True`） でフィルタリングし、
    # JSONL header・実行アーカイブ・formatter ctxへ渡すcommandsを「実際に実行されるもの」に統一する。
    commands = [c for c in commands if config.values.get(c) is True]

    # retry_command再構成用のベース情報を確定する。original_cwdはrun() が保存した
    # --work-dir適用前のcwd、original_sys_argsは起動時のsys.argv[1:] のコピー。
    effective_cwd = original_cwd if original_cwd is not None else os.getcwd()
    effective_sys_args = list(original_sys_args) if original_sys_args is not None else list(sys.argv[1:])
    launcher_prefix = pyfltr.state.retry.detect_launcher_prefix()
    retry_args_template = pyfltr.state.retry.build_retry_args_template(effective_sys_args)

    # 実行アーカイブの初期化 （既定で有効）。
    # `--no-archive` または `archive = false` で無効化できる。クリーンアップ失敗や
    # 書き込み失敗はパイプライン本体を止めないようwarningsへ転送する。
    archive_enabled = bool(config.values.get("archive", True)) and not getattr(args, "no_archive", False)
    archive_store: pyfltr.state.archive.ArchiveStore | None = None
    run_id: str | None = None
    if archive_enabled:
        try:
            archive_store = pyfltr.state.archive.ArchiveStore()
            run_id = archive_store.start_run(commands=commands, files=len(all_files))
            removed = archive_store.cleanup(pyfltr.state.archive.policy_from_config(config))
            if removed:
                logger.debug("archive: 自動削除で %d 件の古い run を削除", len(removed))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="archive", message=f"実行アーカイブを初期化できません: {e}")
            archive_store = None
            run_id = None

    # 実行環境の情報を出力（run_id採番後にまとめて出力することで区切り線内に含める）。
    text_logger.info(f"{'-' * 10} pyfltr {'-' * (72 - 10 - 8)}")
    text_logger.info(f"version:        {importlib.metadata.version('pyfltr')}")
    text_logger.info(f"sys.executable: {sys.executable}")
    text_logger.info(f"sys.version:    {sys.version}")
    text_logger.info(f"cwd:            {os.getcwd()}")
    if run_id is not None:
        launcher_cmd = shlex.join(launcher_prefix)
        text_logger.info("run_id:         %s(`%s show-run %s` で詳細を確認可能)", run_id, launcher_cmd, run_id)
    text_logger.info("-" * 72)

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
            pyfltr.warnings_.emit_warning(source="cache", message=f"ファイル hash キャッシュを初期化できません: {e}")
            cache_store = None

    archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None
    if archive_store is not None and run_id is not None:
        archive_hook = _make_archive_hook(archive_store, run_id)

    # retry_commandをCommandResultに埋めるためのヘルパー。
    # archive_hookと同じタイミング （各ツール完了時） に呼ばれるon_result経路へ挿入する。
    # 実装本体は `populate_retry_command` （失敗ファイルフィルタリング・cached判定を含む） に
    # 委譲し、コンテキスト変数をファクトリ引数で引き渡す。
    _attach_retry_command = _make_attach_retry_command(
        retry_args_template=retry_args_template,
        launcher_prefix=launcher_prefix,
        original_cwd=effective_cwd,
    )

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.output.ui.can_use_ui())

    # run_pipelineが1回だけ組み立てる不変コンテキスト。
    # archive_storeはhook経由で渡すためContextには含めない。
    base_ctx = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=all_files,
        cache_store=cache_store,
        cache_run_id=run_id,
    )

    # 各ツール完了時のフック: retry_command付与 → archive書き込み → formatter.on_result （ストリーミング等）。
    # retry_commandはarchiveとJSONL streamingの双方で必要になるため、archive_hookより前に挿入する。
    # formatter.on_resultはarchive_hookの後に呼ぶ（result.archived=Trueが設定された後）。
    # on_start / on_result / on_finishで使う完全なctxを構築する。
    per_command_log = bool(args.stream)
    include_details_from_stream = not per_command_log
    ctx = pyfltr.output.formatters.RunOutputContext(
        config=config,
        output_file=output_file,
        force_text_on_stderr=force_text_on_stderr,
        commands=commands,
        all_files=len(all_files),
        run_id=run_id,
        launcher_prefix=launcher_prefix,
        retry_args_template=retry_args_template,
        stream=per_command_log,
        include_details=include_details_from_stream,
        structured_stdout=structured_stdout,
        format_source=format_source,
    )

    formatter.on_start(ctx)

    # heartbeat監視の起動。
    # `jsonl`形式のときに限定して起動する。
    # `sarif`・`code-quality`はbufferingformatter（`on_finish`で単一JSONドキュメントを一括出力）のため、
    # 途中で`status:"running"`レコードを混入させると最終的な出力（SARIF 2.1.0オブジェクト・
    # Code Climate JSON配列）が不正な形式になる。
    # `text`等のJSONLレコードが流れない出力形式ではheartbeatの観測対象が成立しない。
    # TUI経路はUIに進捗表示があるため別途heartbeat不要。
    heartbeat: HeartbeatMonitor | None = None
    if output_format == "jsonl" and not use_ui:
        heartbeat = HeartbeatMonitor()
        heartbeat.start()

    # 各ツール完了時のフック順序:
    #   1. _attach_retry_command(result) → retry_commandをresultに付与
    #   2. archive_hook(result) → アーカイブ書き込み（cachedの場合はスキップ）
    #   3. formatter.on_result(ctx, result) → JSONL streamingなど（cachedでも呼ばれる）
    # 上記1+2をcomposed_hookにまとめ、3はrun_commands_with_cliのon_result引数として渡す。
    # これによりcachedの場合でもformatter.on_resultが呼ばれる（cli.pyの設計を踏襲）。
    composed_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None
    if archive_hook is not None:

        def _composed_archive_hook(result: pyfltr.command.core_.CommandResult) -> None:
            _attach_retry_command(result)
            archive_hook(result)

        composed_hook = _composed_archive_hook
    else:
        composed_hook = _attach_retry_command

    def _on_result_callback(result: pyfltr.command.core_.CommandResult) -> None:
        formatter.on_result(ctx, result)

    # run
    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    fail_fast = bool(getattr(args, "fail_fast", False))
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
            per_command_log=per_command_log,
            include_fix_stage=include_fix_stage,
            on_result=_on_result_callback,
            archive_hook=composed_hook,
            fail_fast=fail_fast,
            only_failed_targets=only_failed_targets,
            heartbeat=heartbeat,
        )
        returncode = 0

    # heartbeat監視の停止（成果物書き込み前に確実に停止する）。
    if heartbeat is not None:
        heartbeat.stop()

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
        and all(result.status == "skipped" for result in results)
    ):
        returncode = 1

    formatter.on_finish(ctx, results, returncode, pyfltr.warnings_.collected_warnings())

    # アーカイブ終端: meta.jsonにexit_code / finished_atを書き込む。
    if archive_store is not None and run_id is not None:
        try:
            archive_store.finalize_run(run_id, exit_code=returncode, commands=commands, files=len(all_files))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="archive", message=f"meta.json の更新に失敗: {e}")

    # pre-commit経由かつformatter自動修正発生時のMM状態ガイダンスを必要に応じて出力する。
    _maybe_emit_precommit_guidance(results, structured_stdout=structured_stdout)

    return (returncode, run_id)


_PRECOMMIT_MM_MESSAGE: str = (
    "formatterによる自動修正が発生しました。"
    "`git status`で変更を確認し、必要なら`git add`してから`git commit`を再実行してください。"
)


def _maybe_emit_precommit_guidance(
    results: list[pyfltr.command.core_.CommandResult],
    *,
    structured_stdout: bool,
) -> None:
    """pre-commit経由かつformatter修正発生時にMM状態ガイダンスをstderrへ出力する。

    `git commit` から起動されたpre-commit経由でpyfltrがformatterを実行すると、
    修正結果がワークツリーには書き込まれる一方でindexには反映されない （MM状態）。
    この場合に限り `git add` を促すメッセージを人間向け （日本語） で出力する。

    構造化stdoutモード （`jsonl` / `sarif` / `code-quality` をstdoutに出力する） では、
    stderrにtextが既に出力されているため重複を避ける意味でも抑止する。`github-annotations`
    はtextと同じレイアウトをstdoutに出力するため抑止不要。
    """
    if structured_stdout:
        return
    if not any(result.status == "formatted" for result in results):
        return
    if not pyfltr.cli.precommit_guidance.is_invoked_from_git_commit():
        return
    print(_PRECOMMIT_MM_MESSAGE, file=sys.stderr)


def calculate_returncode(results: list[pyfltr.command.core_.CommandResult], exit_zero_even_if_formatted: bool) -> int:
    """終了コードを計算。"""
    statuses = [result.status for result in results]
    if any(status in {"failed", "resolution_failed"} for status in statuses):
        return 1
    if not exit_zero_even_if_formatted and any(status == "formatted" for status in statuses):
        return 1
    return 0


_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(pyfltr.output.formatters.FORMATTERS.keys())


def _flatten_commands_arg(values: list[str] | None, config: pyfltr.config.config.Config) -> list[str]:
    """`--commands` で渡されたリスト（複数回指定の集合）をコマンド名配列に展開する。

    各要素にはカンマ区切りで複数のコマンドを含められるため、splitした上で
    先頭出現を優先した重複除去を行う。`None` の場合は設定上の全登録コマンド
    （ビルトイン + custom-commands）を返す。
    """
    if values is None:
        return list(config.command_names)
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        for name in raw.split(","):
            if name == "" or name in seen:
                continue
            seen.add(name)
            result.append(name)
    return result


def _resolve_output_format(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> pyfltr.cli.output_format.OutputFormatResolution:
    """実行系サブコマンド向けに出力形式を解決する。

    `run-for-agent`のみサブコマンド既定値`"jsonl"`を渡し、`AI_AGENT`検出時は実行系全体で
    `jsonl`既定を採用する。`PYFLTR_OUTPUT_FORMAT=text`での変更は`cli/output_format.py`側で扱う。
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
    original_sys_args: typing.Sequence[str],
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
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as e:
        logger.error(f"設定エラー: {e}")
        return 1

    args.output_format = output_format
    args.format_source = format_source
    args.output_file = output_file

    # カスタムコマンド用のCLI引数を動的追加して再パース。
    # reparse_fnはcli/main.pyが渡すコールバックで、cli/parserへの直接依存を持たずに済む。
    custom_commands = [name for name, info in config.commands.items() if not info.builtin]
    if custom_commands and reparse_fn is not None:
        parser, args = reparse_fn(custom_commands)
        # 再パースで各種属性が初期化されるため、確定済みの値を再適用する。
        args.output_format = output_format
        args.format_source = format_source
        args.output_file = output_file
        if getattr(args, "no_fix", False):
            args.include_fix_stage = False
        if args.ci:
            args.shuffle = False
            args.no_ui = True

    # --work-dir指定時、再パースで上書きされたtargetsを絶対パスで復元
    if resolved_targets is not None:
        args.targets = resolved_targets

    # CLIオプションでconfigを上書き
    if args.jobs is not None:
        config.values["jobs"] = args.jobs
    if args.no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if args.no_gitignore:
        config.values["respect-gitignore"] = False
    if args.human_readable:
        for key in list(config.values):
            if key.endswith("-json") or key == "pytest-tb-line":
                config.values[key] = False

    # --commands未指定時はカスタムコマンドを含む全登録コマンドを対象にする。
    # argparseのデフォルト評価時点ではpyproject.tomlを読み込んでいないため、
    # ビルトインのみのdefaultを返すとcustom-commandsが常にスキップされる。
    # load_config後に実体を決定することで、ユーザーが登録したcustom-commands
    # （例: svelte-check） も `run` / `ci` サブコマンドのデフォルト動作で実行されるようにする。
    # `--commands` は `action="append"` によりリストで渡るため、各要素を
    # カンマ区切りで再分割して平坦化する。重複は先出を優先して除去する。
    commands: list[str] = pyfltr.config.config.resolve_aliases(_flatten_commands_arg(args.commands, config), config)
    for command in commands:
        if command not in config.values:
            parser.error(f"コマンドが見つかりません: {command}")

    exit_code, _run_id = run_pipeline(
        args, commands, config, original_cwd=original_cwd, original_sys_args=list(original_sys_args)
    )
    return exit_code
