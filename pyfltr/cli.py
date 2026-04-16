"""コマンドライン処理。"""

import argparse
import concurrent.futures
import logging
import pathlib
import shlex
import threading
import typing

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.executor
import pyfltr.llm_output

NCOLS = 128

logger = logging.getLogger(__name__)

lock = threading.Lock()


def run_commands_with_cli(
    commands: list[str],
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    all_files: list[pathlib.Path],
    *,
    per_command_log: bool,
    include_fix_stage: bool = False,
    on_result: typing.Callable[[pyfltr.command.CommandResult], None] | None = None,
) -> list[pyfltr.command.CommandResult]:
    """コマンドを実行する (非 TUI)。

    `per_command_log=True` のときは各コマンド完了時に詳細ログを即時出力する (`--stream` 相当)。
    `per_command_log=False` のときは完了時に 1 行進捗のみを出し、詳細はバッファに残す。
    いずれの場合も、呼び出し側で最後に `render_results()` を呼ぶことで
    summary と詳細ログをまとめて出力できる。

    ``include_fix_stage=True`` のとき、fix-args 定義済みコマンドを先に ``--fix`` 付きで
    直列実行してから、formatter → linter/tester の順で通常実行に進む
    （``ruff check --fix → ruff format → ruff check`` と同じ 2 段階方式の一般化）。

    ``on_result`` が指定されている場合、各コマンド完了時にコールバックを呼び出す。
    JSONL stdoutモードでのストリーミング出力に使用する。
    """
    results: list[pyfltr.command.CommandResult] = []
    fixers, formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(
        commands, config, all_files, include_fix_stage=include_fix_stage
    )

    # fix ステージ: 同一ファイルへの書き込み競合を避けるため直列実行する。
    # 結果は summary / jsonl には含めない（後段の通常ステージで同一コマンドが
    # 再度走って最終状態を報告するため。ruff-format の 2 段階と同じ位置づけ）。
    for command in fixers:
        _run_one_command(command, args, config, all_files, per_command_log=per_command_log, fix_stage=True)

    # formatters を順序実行
    for command in formatters:
        result = _run_one_command(command, args, config, all_files, per_command_log=per_command_log)
        results.append(result)
        if on_result is not None:
            on_result(result)

    # linters/testers を並列実行
    if len(linters_and_testers) > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config["jobs"]) as executor:
            future_to_command = {
                executor.submit(_run_one_command, command, args, config, all_files, per_command_log=per_command_log): command
                for command in linters_and_testers
            }
            for future in concurrent.futures.as_completed(future_to_command):
                result = future.result()
                results.append(result)
                if on_result is not None:
                    on_result(result)

    return results


def _run_one_command(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    all_files: list[pathlib.Path],
    *,
    per_command_log: bool,
    fix_stage: bool = False,
) -> pyfltr.command.CommandResult:
    """1 コマンドの実行。

    `per_command_log=True` ならば完了直後に詳細ログを `write_log()` で出す。
    それ以外は開始/完了の 1 行進捗のみ出力する。
    """
    # serial_group を持つコマンドは同一グループ内で排他実行される (cargo / dotnet 等)
    with pyfltr.executor.serial_group_lock(config.commands[command].serial_group):
        with lock:
            suffix = " (fix)" if fix_stage else ""
            logger.info(f"{command}{suffix} 実行中です...")
        result = pyfltr.command.execute_command(command, args, config, all_files, fix_stage=fix_stage)
        if per_command_log:
            write_log(result)
        else:
            with lock:
                logger.info(f"{command}{suffix} 完了 ({result.get_status_text()})")
        return result


def write_log(result: pyfltr.command.CommandResult) -> None:
    """コマンド実行結果の詳細ログ出力。

    パース済みエラーがある場合は format_error() で整形した一覧を表示する。
    エラーがなく失敗した場合は生出力をフォールバック表示する。
    """
    mark = "@" if result.alerted else "*"
    with lock:
        logger.info(f"{mark * 32} {result.command} {mark * (NCOLS - 34 - len(result.command))}")
        logger.debug(f"{mark} commandline: {shlex.join(result.commandline)}")
        logger.info(mark)
        if result.errors:
            for error in result.errors:
                logger.info(pyfltr.error_parser.format_error(error))
        elif result.alerted:
            logger.info(result.output)
        else:
            summary = pyfltr.error_parser.parse_summary(result.command, result.output)
            if summary:
                logger.info(f"{mark} {summary}")
        logger.info(mark)
        logger.info(f"{mark} returncode: {result.returncode}")
        logger.info(mark * NCOLS)


def render_results(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    include_details: bool,
    output_format: str = "text",
    output_file: pathlib.Path | None = None,
    exit_code: int = 0,
    warnings: list[dict[str, typing.Any]] | None = None,
) -> None:
    """実行結果を `成功コマンド → 失敗コマンド → summary` の順でまとめて出力する。

    summary を末尾に出力することで、`tail -N` で末尾だけ読み取るツール
    (Claude Code など) でも summary が確実に見えるようにする。失敗コマンド詳細も
    summary の直前に置くため、`tail -N` でエラー情報も捕捉しやすい。

    `include_details=False` のときは、詳細ログは既に出力済みとみなし summary のみ表示する
    (`--stream` モード向け)。

    `output_format="jsonl"` のときは `pyfltr.llm_output.write_jsonl()` に委譲する。
    `output_file` が None なら stdout に書き、text 経路は通らない
    (呼び出し元で logging が抑止されている前提)。`output_file` が指定されている場合は
    そのファイルに JSONL を書いたうえで、stdout には従来の text 出力も継続する。
    """
    ordered = sorted(results, key=lambda r: config.command_names.index(r.command))
    warnings = warnings or []

    if output_format == "jsonl":
        pyfltr.llm_output.write_jsonl(ordered, config, exit_code=exit_code, destination=output_file, warnings=warnings)
        if output_file is None:
            return

    if include_details:
        # 1. 成功コマンドの詳細ログ
        for result in ordered:
            if not result.alerted:
                write_log(result)

        # 2. 失敗コマンドの詳細ログ (summary の直前に配置し tail -N でも拾えるようにする)
        for result in ordered:
            if result.alerted:
                write_log(result)

    # 3. warnings (summary の直前。先頭だと見落とされやすいため)
    _write_warnings_section(warnings)

    # 4. summary (末尾に出力することで tail -N で必ず見えるようにする)
    _write_summary(ordered)


def _write_warnings_section(warnings: list[dict[str, typing.Any]]) -> None:
    """Warnings セクションを summary 直前に出力する。"""
    if not warnings:
        return
    with lock:
        logger.info(f"{'-' * 10} warnings {'-' * (72 - 10 - 10)}")
        for entry in warnings:
            logger.info(f"    [{entry['source']}] {entry['message']}")


def _write_summary(ordered_results: list[pyfltr.command.CommandResult]) -> None:
    """Summary セクションを出力する。"""
    with lock:
        logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
        for result in ordered_results:
            logger.info(f"    {result.command:<16s} {result.get_status_text()}")
        logger.info("-" * 72)
