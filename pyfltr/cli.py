"""コマンドライン処理。"""

import argparse
import concurrent.futures
import logging
import shlex
import threading

import pyfltr.command
import pyfltr.config
import pyfltr.executor

NCOLS = 128

logger = logging.getLogger(__name__)

lock = threading.Lock()


def run_commands_with_cli(
    commands: list[str],
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    *,
    per_command_log: bool,
) -> list[pyfltr.command.CommandResult]:
    """コマンドを実行する (非 TUI)。

    `per_command_log=True` のときは各コマンド完了時に詳細ログを即時出力する (`--stream` 相当)。
    `per_command_log=False` のときは完了時に 1 行進捗のみを出し、詳細はバッファに残す。
    いずれの場合も、呼び出し側で最後に `render_results()` を呼ぶことで
    summary と詳細ログをまとめて出力できる。
    """
    results: list[pyfltr.command.CommandResult] = []
    formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(
        commands, config, fix_mode=bool(getattr(args, "fix", False))
    )

    # formatters を順序実行
    for command in formatters:
        results.append(_run_one_command(command, args, config, per_command_log=per_command_log))

    # linters/testers を並列実行
    if len(linters_and_testers) > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config["jobs"]) as executor:
            future_to_command = {
                executor.submit(_run_one_command, command, args, config, per_command_log=per_command_log): command
                for command in linters_and_testers
            }
            for future in concurrent.futures.as_completed(future_to_command):
                results.append(future.result())

    return results


def _run_one_command(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    *,
    per_command_log: bool,
) -> pyfltr.command.CommandResult:
    """1 コマンドの実行。

    `per_command_log=True` ならば完了直後に詳細ログを `write_log()` で出す。
    それ以外は開始/完了の 1 行進捗のみ出力する。
    """
    with lock:
        logger.info(f"{command} 実行中です...")
    result = pyfltr.command.execute_command(command, args, config)
    if per_command_log:
        write_log(result)
    else:
        with lock:
            logger.info(f"{command} 完了 ({result.get_status_text()})")
    return result


def write_log(result: pyfltr.command.CommandResult) -> None:
    """コマンド実行結果の詳細ログ出力。"""
    mark = "@" if result.alerted else "*"
    with lock:
        logger.info(f"{mark * 32} {result.command} {mark * (NCOLS - 34 - len(result.command))}")
        logger.debug(f"{mark} commandline: {shlex.join(result.commandline)}")
        logger.info(mark)
        logger.info(result.output)
        logger.info(mark)
        logger.info(f"{mark} returncode: {result.returncode}")
        logger.info(mark * NCOLS)


def render_results(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    include_details: bool,
) -> None:
    """実行結果を `summary → 成功コマンド → 失敗コマンド` の順でまとめて出力する。

    `include_details=False` のときは、詳細ログは既に出力済みとみなし summary のみ表示する
    (`--stream` モード向け)。
    """
    ordered = sorted(results, key=lambda r: config.command_names.index(r.command))

    # 1. summary
    _write_summary(ordered)

    if not include_details:
        return

    # 2. 成功コマンドの詳細ログ
    for result in ordered:
        if not result.alerted:
            write_log(result)

    # 3. 失敗コマンドの詳細ログ (末尾に残すことで tail -N 時にエラーが見切れにくくする)
    for result in ordered:
        if result.alerted:
            write_log(result)


def _write_summary(ordered_results: list[pyfltr.command.CommandResult]) -> None:
    """Summary セクションを出力する。"""
    with lock:
        logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
        for result in ordered_results:
            logger.info(f"    {result.command:<16s} {result.get_status_text()}")
        logger.info("-" * 72)
