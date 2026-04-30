"""パイプライン実行と結果整形。

非TUI経路でのコマンド実行（`run_commands_with_cli`）と、
実行結果のtext整形出力（`render_results` / `write_log`）、
パイプライン全体を駆動する`_run_impl`・`run_pipeline`・
`calculate_returncode`を担う。
"""
# ui.pyとの残余重複（aborted_commands後処理）はcall_from_thread差異のため共通化不可
# pylint: disable=duplicate-code

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
import typing

import pyfltr.cli.output_format
import pyfltr.command.core
import pyfltr.command.dispatcher
import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.output.formatters
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.state.executor
import pyfltr.state.only_failed
import pyfltr.state.retry
import pyfltr.state.stage_runner
import pyfltr.warnings_

NCOLS = 128

logger = logging.getLogger(__name__)

# text_logger / structured_logger は cli/output_format.py で定義する。
# 本モジュールでは output_format から参照して使う。
text_logger = pyfltr.cli.output_format.text_logger
structured_logger = pyfltr.cli.output_format.structured_logger
lock = threading.Lock()


def run_commands_with_cli(
    commands: list[str],
    args: argparse.Namespace,
    base_ctx: pyfltr.command.core.ExecutionBaseContext,
    *,
    per_command_log: bool,
    include_fix_stage: bool = False,
    on_result: typing.Callable[[pyfltr.command.core.CommandResult], None] | None = None,
    archive_hook: typing.Callable[[pyfltr.command.core.CommandResult], None] | None = None,
    fail_fast: bool = False,
    only_failed_targets: dict[str, pyfltr.state.only_failed.ToolTargets] | None = None,
) -> list[pyfltr.command.core.CommandResult]:
    """コマンドを実行する (非 TUI)。

    `per_command_log=True`のときは各コマンド完了時に詳細ログを即時出力する（`--stream`相当）。
    `per_command_log=False`のときは完了時に1行進捗のみを出し、詳細はバッファに残す。
    いずれの場合も、呼び出し側で最後に`render_results()`を呼ぶことで
    summaryと詳細ログをまとめて出力できる。

    `include_fix_stage=True`のとき、fix-args定義済みコマンドを先に`--fix`付きで
    直列実行してから、formatter → linter/testerの順で通常実行に進む
    （`ruff check --fix → ruff format → ruff check`と同じ2段階方式の一般化）。

    `on_result`が指定されている場合、各コマンド完了時にコールバックを呼び出す。
    JSONL stdoutモードでのストリーミング出力に使用する。

    `archive_hook`が指定されている場合、各コマンド完了時に実行アーカイブへ書き出す。
    fixステージの結果はsummaryに含めないが、アーカイブには通常ステージ以外も含めて
    全実行を保存するためfixステージからも`archive_hook`を呼び出す。

    `base_ctx`はパイプライン全体で不変のコンテキスト（config・all_files・cache_store・
    cache_run_idを含む）。各コマンド実行前に`ExecutionContext`を組み立てて渡す。

    `fail_fast=True`のとき、いずれかのツール完了時に`has_error=True`を検出した
    時点で未開始のジョブを`future.cancel()`で打ち切り、起動済みサブプロセスに
    `terminate()`を送る。formatterの`formatted`はfailureに含めない。

    `only_failed_targets`が指定された場合、ツール別の失敗ファイル集合を
    `execute_command`へ流す（`--only-failed`経路で直前runの失敗ファイルのみを
    対象とする）。値が`None`のツールは通常の`all_files`で実行し、`list`の
    ツールはその集合のみを対象にする。
    """
    config = base_ctx.config
    results: list[pyfltr.command.core.CommandResult] = []
    fixers, formatters, linters_and_testers = pyfltr.state.executor.split_commands_for_execution(
        commands, config, base_ctx.all_files, include_fix_stage=include_fix_stage
    )

    # fixステージ: 同一ファイルへの書き込み競合を避けるため直列実行する。
    # 結果はsummary / jsonlには含めない（後段の通常ステージで同一コマンドが
    # 再度走って最終状態を報告するため。ruff-formatの2段階と同じ位置づけ）。
    for command in fixers:
        fix_result = _run_one_command(
            command,
            args,
            base_ctx,
            per_command_log=per_command_log,
            fix_stage=True,
            only_failed_targets=pyfltr.command.targets.pick_targets(only_failed_targets, command),
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
    results: list[pyfltr.command.core.CommandResult],
    *,
    remaining: list[str],
    config: pyfltr.config.config.Config,
    on_result: typing.Callable[[pyfltr.command.core.CommandResult], None] | None,
    archive_hook: typing.Callable[[pyfltr.command.core.CommandResult], None] | None,
) -> list[pyfltr.command.core.CommandResult]:
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
    base_ctx: pyfltr.command.core.ExecutionBaseContext,
    *,
    per_command_log: bool,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.state.only_failed.ToolTargets | None = None,
) -> pyfltr.command.core.CommandResult:
    """1 コマンドの実行。

    `per_command_log=True`ならば完了直後に詳細ログを`write_log()`で出す。
    それ以外は開始/完了の1行進捗のみ出力する。
    """
    # serial_groupを持つコマンドは同一グループ内で排他実行される（cargo / dotnet等）
    with pyfltr.state.executor.serial_group_lock(base_ctx.config.commands[command].serial_group):
        with lock:
            suffix = " (fix)" if fix_stage else ""
            text_logger.info(f"{command}{suffix} 実行中です...")
        ctx = pyfltr.command.core.ExecutionContext(
            base=base_ctx,
            fix_stage=fix_stage,
            only_failed_targets=only_failed_targets,
        )
        result = pyfltr.command.dispatcher.execute_command(command, args, ctx)
        if per_command_log:
            use_ga = (getattr(args, "output_format", "text") or "text") == "github-annotations"
            write_log(result, use_github_annotations=use_ga)
        else:
            with lock:
                text_logger.info(f"{command}{suffix} 完了 ({result.get_status_text()})")
        return result


def write_log(result: pyfltr.command.core.CommandResult, *, use_github_annotations: bool = False) -> None:
    """コマンド実行結果の詳細ログ出力。

    パース済みエラーがある場合は`format_error()`で整形した一覧を表示する。
    エラーがなく失敗した場合は生出力をフォールバック表示する。

    `use_github_annotations`がTrueのとき、ErrorLocation行をGAワークフローコマンド記法で出す。
    False（既定）のときは従来のテキスト形式（`file:line:col: [tool:rule] msg`）で出す。
    枠線・区切り線・進捗ラベルは常にtext記法を維持する
    （GAはエラー箇所の解釈だけを切り替え、レイアウトはtextと同じにする設計）。
    """
    mark = "@" if result.alerted else "*"
    with lock:
        text_logger.info(f"{mark * 32} {result.command} {mark * (NCOLS - 34 - len(result.command))}")
        logger.debug(f"{mark} commandline: {shlex.join(result.commandline)}")
        text_logger.info(mark)
        if result.errors:
            for error in result.errors:
                if use_github_annotations:
                    text_logger.info(pyfltr.command.error_parser.format_error_github(error))
                else:
                    text_logger.info(pyfltr.command.error_parser.format_error(error))
        elif result.alerted:
            text_logger.info(result.output)
        else:
            summary = pyfltr.command.error_parser.parse_summary(result.command, result.output)
            if summary:
                text_logger.info(f"{mark} {summary}")
        text_logger.info(mark)
        text_logger.info(f"{mark} returncode: {result.returncode}")
        text_logger.info(mark * NCOLS)


def render_results(
    results: list[pyfltr.command.core.CommandResult],
    config: pyfltr.config.config.Config,
    *,
    include_details: bool,
    output_format: str = "text",
    exit_code: int = 0,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
) -> None:
    """実行結果を `成功コマンド → 失敗コマンド → summary` の順でまとめて出力する。

    summaryを末尾に出力することで、`tail -N`で末尾だけ読み取るツール
    （Claude Codeなど）でもsummaryが確実に見えるようにする。失敗コマンド詳細も
    summaryの直前に置くため、`tail -N`でエラー情報も捕捉しやすい。

    `include_details=False`のときは、詳細ログは既に出力済みとみなしsummaryのみ表示する
    （`--stream`モード向け）。

    構造化出力（JSONL / SARIF）はここでは扱わず、呼び出し元（`pyfltr.cli.main`）が
    `structured_logger`経由で書き出す。本関数は常にtext整形ログを
    `text_logger`に流す。`output_format`はErrorLocation行の整形方式の
    切替（`github-annotations`時のみGA記法）に使う。
    """
    del exit_code, commands, files, run_id, launcher_prefix  # 構造化出力への委譲が無くなり未使用
    ordered = sorted(results, key=lambda r: config.command_names.index(r.command))
    warnings = warnings or []

    use_ga = output_format == "github-annotations"
    if include_details:
        # 1. 成功コマンドの詳細ログ
        for result in ordered:
            if not result.alerted:
                write_log(result, use_github_annotations=use_ga)

        # 2. 失敗コマンドの詳細ログ（summaryの直前に配置しtail -Nでも拾えるようにする）
        for result in ordered:
            if result.alerted:
                write_log(result, use_github_annotations=use_ga)

    # 3. warnings（summaryの直前。先頭だと見落とされやすいため）
    _write_warnings_section(warnings)

    # 4. fully excluded files（summary直前。警告と混ざらないよう独立ブロックで出す）
    _write_fully_excluded_files_section(pyfltr.warnings_.excluded_direct_files())

    # 5. summary（末尾に出力することでtail -Nで必ず見えるようにする）
    _write_summary(ordered)


def _write_warnings_section(warnings: list[dict[str, typing.Any]]) -> None:
    """Warningsセクションをsummary直前に出力する。"""
    if not warnings:
        return
    with lock:
        text_logger.info(f"{'-' * 10} warnings {'-' * (72 - 10 - 10)}")
        for entry in warnings:
            text_logger.info(f"    [{entry['source']}] {entry['message']}")


def _write_fully_excluded_files_section(files: list[str]) -> None:
    """直接指定されたが除外設定で全除外されたファイルをまとめて表示する。

    警告としては個別のwarning行で既に通知しているが、総覧で見落とされやすいため
    summary直前に専用ブロックを置く。exit コードには影響しない。
    """
    if not files:
        return
    with lock:
        text_logger.info(f"{'-' * 10} fully-excluded-files {'-' * (72 - 10 - 22)}")
        for path in files:
            text_logger.info(f"    {path}")


def _write_summary(ordered_results: list[pyfltr.command.core.CommandResult]) -> None:
    """Summary セクションを出力する。"""
    with lock:
        text_logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
        for result in ordered_results:
            text_logger.info(f"    {result.command:<16s} {result.get_status_text()}")
        text_logger.info("-" * 72)


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
    # format / output_file / force_text_on_stderrの組み合わせで出力先が切り替わるため、毎回張り直す。
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

    # --changed-since指定時はgit差分ファイルとの交差に絞り込む。
    # --only-failedよりも先に適用し、以後のフィルタは絞り込み済みリストを受け取る。
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
    # split_commands_for_executionと同じ条件 （`config.values.get(cmd) is True`） で絞り込み、
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
    # 書き込み失敗はパイプライン本体を止めないようwarningsへ流す。
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

    # 実行環境の情報を出力（run_id採番後にまとめて出すことで区切り線内に含める）。
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
    # 書き込み失敗はパイプライン本体を止めないためwarningsに流す。
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

    archive_hook: typing.Callable[[pyfltr.command.core.CommandResult], None] | None = None
    if archive_store is not None and run_id is not None:
        captured_store = archive_store
        captured_run_id = run_id

        def _archive_hook(result: pyfltr.command.core.CommandResult) -> None:
            try:
                captured_store.write_tool_result(captured_run_id, result)
            except OSError as e:
                # ハンドラ内でwarningを出してもsummary末尾にまとまる。
                pyfltr.warnings_.emit_warning(source="archive", message=f"{result.command} のアーカイブ書き込みに失敗: {e}")
                return
            # 書き込み成功時のみarchived=Trueに更新。smart truncationの可否判定に使う。
            result.archived = True

        archive_hook = _archive_hook

    # retry_commandをCommandResultに埋めるためのヘルパー。
    # archive_hookと同じタイミング （各ツール完了時） に呼ばれるon_result経路へ挿入する。
    # 実装本体は `_populate_retry_command` （A案の失敗ファイル絞り込み・cached
    # 判定を含む） に委譲し、クロージャ変数をキーワード引数で引き渡す。
    def _attach_retry_command(result: pyfltr.command.core.CommandResult) -> None:
        pyfltr.state.retry.populate_retry_command(
            result,
            retry_args_template=retry_args_template,
            launcher_prefix=launcher_prefix,
            original_cwd=effective_cwd,
        )

    # UIの判定
    from pyfltr.output import ui as _ui_module  # pylint: disable=import-outside-toplevel

    use_ui = not args.no_ui and (args.ui or _ui_module.can_use_ui())

    # run_pipelineが1回だけ組み立てる不変コンテキスト。
    # archive_storeはhook経由で渡すためContextには含めない。
    base_ctx = pyfltr.command.core.ExecutionBaseContext(
        config=config,
        all_files=all_files,
        cache_store=cache_store,
        cache_run_id=run_id,
    )

    # 各ツール完了時のフック: retry_command付与 → archive書き込み → formatter.on_result （ストリーミング等）。
    # retry_commandはarchiveとJSONL streamingの双方で必要になるため、archive_hookより前に挿入する。
    # formatter.on_resultはarchive_hookの後に呼ぶ（result.archived=Trueが立った後）。
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

    # 各ツール完了時のフック順序:
    #   1. _attach_retry_command(result) → retry_commandをresultに付与
    #   2. archive_hook(result) → アーカイブ書き込み（cachedの場合はスキップ）
    #   3. formatter.on_result(ctx, result) → JSONL streamingなど（cachedでも呼ばれる）
    # 上記1+2をcomposed_hookにまとめ、3はrun_commands_with_cliのon_result引数として渡す。
    # これによりcachedの場合でもformatter.on_resultが呼ばれる（cli.pyの設計を踏襲）。
    composed_hook: typing.Callable[[pyfltr.command.core.CommandResult], None] | None = None
    if archive_hook is not None:

        def _composed_archive_hook(result: pyfltr.command.core.CommandResult) -> None:
            _attach_retry_command(result)
            archive_hook(result)

        composed_hook = _composed_archive_hook
    else:
        composed_hook = _attach_retry_command

    def _on_result_callback(result: pyfltr.command.core.CommandResult) -> None:
        formatter.on_result(ctx, result)

    # run
    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    fail_fast = bool(getattr(args, "fail_fast", False))
    if use_ui:
        results, returncode = _ui_module.run_commands_with_ui(
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
        )
        returncode = 0

    # returncodeを先に確定させる （render_resultsに渡してJSONL summary.exitに埋めるため）
    # TUIのCtrl+C協調停止は `run_commands_with_ui` から130 （SIGINT慣例） を返す。
    # この場合は `calculate_returncode` で上書きせず、そのまま採用する。
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)

    formatter.on_finish(ctx, results, returncode, pyfltr.warnings_.collected_warnings())

    # アーカイブ終端: meta.jsonにexit_code / finished_atを書き込む。
    if archive_store is not None and run_id is not None:
        try:
            archive_store.finalize_run(run_id, exit_code=returncode, commands=commands, files=len(all_files))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="archive", message=f"meta.json の更新に失敗: {e}")

    # pre-commit経由かつformatter自動修正発生時のMM状態ガイダンスを必要に応じて出す。
    _maybe_emit_precommit_guidance(results, structured_stdout=structured_stdout)

    return (returncode, run_id)


_PRECOMMIT_MM_MESSAGE: str = (
    "formatterによる自動修正が発生しました。"
    "`git status`で変更を確認し、必要なら`git add`してから`git commit`を再実行してください。"
)


def _maybe_emit_precommit_guidance(
    results: list[pyfltr.command.core.CommandResult],
    *,
    structured_stdout: bool,
) -> None:
    """pre-commit経由かつformatter修正発生時にMM状態ガイダンスをstderrへ出す。

    `git commit` から起動されたpre-commit経由でpyfltrがformatterを走らせると、
    修正結果がワークツリーには書き込まれる一方でindexには反映されない （MM状態）。
    この場合に限り `git add` を促すメッセージを人間向け （日本語） で出力する。

    構造化stdoutモード （`jsonl` / `sarif` / `code-quality` をstdoutに流す） では、
    stderrにtextが既に流れているため重複を避ける意味でも抑止する。`github-annotations`
    はtextと同じレイアウトをstdoutに出すため抑止不要。
    """
    if structured_stdout:
        return
    if not any(result.status == "formatted" for result in results):
        return
    from pyfltr.cli import precommit_guidance as _precommit  # pylint: disable=import-outside-toplevel

    if not _precommit.is_invoked_from_git_commit():
        return
    print(_PRECOMMIT_MM_MESSAGE, file=sys.stderr)


def calculate_returncode(results: list[pyfltr.command.core.CommandResult], exit_zero_even_if_formatted: bool) -> int:
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
    `jsonl`既定を採用する。`PYFLTR_OUTPUT_FORMAT=text`での切り戻しは`cli/output_format.py`側で扱う。
    """
    subcommand_default = "jsonl" if args.subcommand == "run-for-agent" else None
    return pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=_VALID_OUTPUT_FORMATS,
        subcommand_default=subcommand_default,
        ai_agent_default="jsonl",
    )


def _run_impl(
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
    # 「再実行対象を指定runの失敗ツールに絞り込む」という本来の意味を持たない。
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
    # （例: svelte-check） も `run` / `ci` サブコマンドのデフォルト動作で走るようにする。
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
