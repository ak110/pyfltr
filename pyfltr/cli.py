"""コマンドライン処理。"""
# ui.pyとの残余重複（aborted_commands後処理）はcall_from_thread差異のため共通化不可
# pylint: disable=duplicate-code

from __future__ import annotations

import argparse
import collections.abc
import concurrent.futures
import logging
import os
import pathlib
import shlex
import threading
import typing

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.executor
import pyfltr.stage_runner
import pyfltr.warnings_

if typing.TYPE_CHECKING:
    # only_failedは`pyfltr.cli.text_logger`を遅延参照で呼ぶため、モジュール間の
    # 循環importを避けるために型チェック時のみimportする。
    import pyfltr.only_failed

NCOLS = 128

logger = logging.getLogger(__name__)

# 人間向けテキスト出力用の専用logger（進捗・詳細ログ・summary・warnings・`--only-failed`案内）。
# system logger（root）と分離することで、format別に出力先（stdout / stderr）と
# ログレベルを独立に切り替えられる。propagate=Falseでrootへのpropagateを止め、
# rootのstderrハンドラーと重複発火しないようにする。
text_logger = logging.getLogger("pyfltr.textout")
text_logger.propagate = False

# 構造化出力（JSONL / SARIF）用の専用logger。出力先は`configure_structured_output`で
# StreamHandler（stdout）またはFileHandler（`--output-file`）に切り替える。
# propagate=Falseでroot経由の二重出力とlevel継承の副作用を防ぐ。
structured_logger = logging.getLogger("pyfltr.structured")
structured_logger.propagate = False

lock = threading.Lock()


OUTPUT_FORMAT_ENV = "PYFLTR_OUTPUT_FORMAT"
"""出力形式を環境変数で既定指定するためのキー名。"""

AI_AGENT_ENV = "AI_AGENT"
"""エージェント実行を示す慣習的な環境変数名。"""

_AI_AGENT_DEFAULT_FORMAT = "jsonl"
"""AI_AGENT検出時に採用する出力形式。"""


def resolve_output_format(
    parser: argparse.ArgumentParser,
    cli_value: str | None,
    *,
    valid_values: collections.abc.Set[str],
    subcommand_default: str | None = None,
    final_default: str = "text",
) -> str:
    """出力形式を共通の優先順位で決定する。

    優先順位は「CLI > `PYFLTR_OUTPUT_FORMAT` > サブコマンド既定値 > `AI_AGENT(jsonl)` > 最終既定値」。
    CLI明示値（`cli_value`）と`PYFLTR_OUTPUT_FORMAT`は利用者が意識的に指定した値とみなし、
    サブコマンド既定値・`AI_AGENT`検出より優先する。これによりエージェント環境下や
    `run-for-agent`配下でも`PYFLTR_OUTPUT_FORMAT=text`で切り戻せる。

    Args:
        parser: 環境変数バリデーションエラー時の`parser.error`呼び出しに使う。
        cli_value: CLIで明示された`--output-format`の値。未指定時は`None`。
        valid_values: サブコマンドが受理する出力形式集合。`PYFLTR_OUTPUT_FORMAT`の値検証と、
            サブコマンド既定値・`AI_AGENT(jsonl)`の採否判定に使う。
        subcommand_default: サブコマンド固有の既定値（例: `run-for-agent`では`"jsonl"`）。
            `valid_values`に含まれない場合は無視する。`None`の場合は次段階へ進む。
        final_default: いずれの解決経路にも該当しない場合の最終既定値。

    Returns:
        解決済みの出力形式。

    `AI_AGENT`は環境変数が設定されていれば真扱い（空文字列は未設定扱い、値の中身は問わない）。
    `AI_AGENT`既定値（`jsonl`）が`valid_values`に含まれない場合は無視し、利用者から見て予期しない
    フォールバックを起こさない。
    """
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(OUTPUT_FORMAT_ENV)
    if env_value is not None and env_value != "":
        if env_value not in valid_values:
            parser.error(
                f"環境変数 {OUTPUT_FORMAT_ENV} に不正な値が指定されています: {env_value!r} "
                f"(有効値: {', '.join(sorted(valid_values))})"
            )
        return env_value
    if subcommand_default is not None and subcommand_default in valid_values:
        return subcommand_default
    ai_agent_value = os.environ.get(AI_AGENT_ENV)
    if ai_agent_value is not None and ai_agent_value != "" and _AI_AGENT_DEFAULT_FORMAT in valid_values:
        return _AI_AGENT_DEFAULT_FORMAT
    return final_default


def configure_text_output(stream: typing.TextIO, *, level: int = logging.INFO) -> None:
    """text_logger の出力先とログレベルを差し替える。

    既存ハンドラーを全て外してから`StreamHandler(stream)`を新規追加する。
    同一プロセス内で`run()`が複数回呼ばれるケースに備えて、呼び出し毎に完全に
    再構築する（古いハンドラーが残って二重出力・古いstream参照が残るのを避ける）。
    """
    for existing in list(text_logger.handlers):
        text_logger.removeHandler(existing)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    text_logger.addHandler(handler)
    text_logger.setLevel(level)


def configure_structured_output(destination: typing.TextIO | pathlib.Path | None) -> None:
    """structured_logger の出力先を切り替える。

    - `None`: ハンドラーを全て外す（jsonl/sarifを出さないformat向け）
    - `TextIO`: `StreamHandler(destination)`を設定する
    - `pathlib.Path`: `FileHandler(destination, mode="w", encoding="utf-8")`を設定する。
      親ディレクトリは自動作成する

    levelは常に`logging.INFO`で固定する。root loggerがWARNING初期化でも
    structured_logger側はINFO記録を破棄しないようにするため。
    """
    for existing in list(structured_logger.handlers):
        structured_logger.removeHandler(existing)
        if isinstance(existing, logging.FileHandler):
            existing.close()
    if destination is None:
        structured_logger.setLevel(logging.INFO)
        return
    handler: logging.Handler
    if isinstance(destination, pathlib.Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(destination, mode="w", encoding="utf-8")
    else:
        handler = logging.StreamHandler(destination)
    handler.setFormatter(logging.Formatter("%(message)s"))
    structured_logger.addHandler(handler)
    structured_logger.setLevel(logging.INFO)


def run_commands_with_cli(
    commands: list[str],
    args: argparse.Namespace,
    base_ctx: pyfltr.command.ExecutionBaseContext,
    *,
    per_command_log: bool,
    include_fix_stage: bool = False,
    on_result: typing.Callable[[pyfltr.command.CommandResult], None] | None = None,
    archive_hook: typing.Callable[[pyfltr.command.CommandResult], None] | None = None,
    fail_fast: bool = False,
    only_failed_targets: dict[str, pyfltr.only_failed.ToolTargets] | None = None,
) -> list[pyfltr.command.CommandResult]:
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
    results: list[pyfltr.command.CommandResult] = []
    fixers, formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(
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
            only_failed_targets=pyfltr.command.pick_targets(only_failed_targets, command),
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
            only_failed_targets=pyfltr.command.pick_targets(only_failed_targets, command),
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
                    only_failed_targets=pyfltr.command.pick_targets(only_failed_targets, command),
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
                    pyfltr.stage_runner.cancel_pending_futures(future_to_command, aborted_commands)
                    pyfltr.command.terminate_active_processes()
            if aborted_commands:
                for pending_command in aborted_commands:
                    skipped = pyfltr.stage_runner.make_skipped_result(pending_command, config)
                    results.append(skipped)
                    if archive_hook is not None:
                        archive_hook(skipped)
                    if on_result is not None:
                        on_result(skipped)

    return results


def _emit_skipped_results(
    results: list[pyfltr.command.CommandResult],
    *,
    remaining: list[str],
    config: pyfltr.config.Config,
    on_result: typing.Callable[[pyfltr.command.CommandResult], None] | None,
    archive_hook: typing.Callable[[pyfltr.command.CommandResult], None] | None,
) -> list[pyfltr.command.CommandResult]:
    """--fail-fast中断時、未実行ツールをskipped扱いで追加する（fix/formatter段から）。"""
    pyfltr.command.terminate_active_processes()
    for command in remaining:
        skipped = pyfltr.stage_runner.make_skipped_result(command, config)
        results.append(skipped)
        if archive_hook is not None:
            archive_hook(skipped)
        if on_result is not None:
            on_result(skipped)
    return results


def _run_one_command(
    command: str,
    args: argparse.Namespace,
    base_ctx: pyfltr.command.ExecutionBaseContext,
    *,
    per_command_log: bool,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.only_failed.ToolTargets | None = None,
) -> pyfltr.command.CommandResult:
    """1 コマンドの実行。

    `per_command_log=True`ならば完了直後に詳細ログを`write_log()`で出す。
    それ以外は開始/完了の1行進捗のみ出力する。
    """
    # serial_groupを持つコマンドは同一グループ内で排他実行される（cargo / dotnet等）
    with pyfltr.executor.serial_group_lock(base_ctx.config.commands[command].serial_group):
        with lock:
            suffix = " (fix)" if fix_stage else ""
            text_logger.info(f"{command}{suffix} 実行中です...")
        ctx = pyfltr.command.ExecutionContext(
            base=base_ctx,
            fix_stage=fix_stage,
            only_failed_targets=only_failed_targets,
        )
        result = pyfltr.command.execute_command(command, args, ctx)
        if per_command_log:
            use_ga = (getattr(args, "output_format", "text") or "text") == "github-annotations"
            write_log(result, use_github_annotations=use_ga)
        else:
            with lock:
                text_logger.info(f"{command}{suffix} 完了 ({result.get_status_text()})")
        return result


def write_log(result: pyfltr.command.CommandResult, *, use_github_annotations: bool = False) -> None:
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
                    text_logger.info(pyfltr.error_parser.format_error_github(error))
                else:
                    text_logger.info(pyfltr.error_parser.format_error(error))
        elif result.alerted:
            text_logger.info(result.output)
        else:
            summary = pyfltr.error_parser.parse_summary(result.command, result.output)
            if summary:
                text_logger.info(f"{mark} {summary}")
        text_logger.info(mark)
        text_logger.info(f"{mark} returncode: {result.returncode}")
        text_logger.info(mark * NCOLS)


def render_results(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
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

    構造化出力（JSONL / SARIF）はここでは扱わず、呼び出し元（`pyfltr.main`）が
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


def _write_summary(ordered_results: list[pyfltr.command.CommandResult]) -> None:
    """Summary セクションを出力する。"""
    with lock:
        text_logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
        for result in ordered_results:
            text_logger.info(f"    {result.command:<16s} {result.get_status_text()}")
        text_logger.info("-" * 72)
