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
) -> list[pyfltr.command.CommandResult]:
    """コマンドの実行。"""
    results: list[pyfltr.command.CommandResult] = []
    formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(commands, config)

    # run formatters (serial)
    for command in formatters:
        results.append(run_command_for_cli(command, args, config))

    # run linters/testers (parallel)
    if len(linters_and_testers) > 0:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(linters_and_testers)) as executor:
            future_to_command = {
                executor.submit(run_command_for_cli, command, args, config): command for command in linters_and_testers
            }
            for future in concurrent.futures.as_completed(future_to_command):
                results.append(future.result())

    return results


def run_command_for_cli(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
) -> pyfltr.command.CommandResult:
    """コマンドの実行（コンソール表示）。"""
    result = pyfltr.command.execute_command(command, args, config)
    write_log(result)
    return result


def write_log(result: pyfltr.command.CommandResult) -> None:
    """ログファイルに書き込む。"""
    mark = "*" if result.returncode == 0 else "@"
    with lock:
        logger.info(f"{mark * 32} {result.command} {mark * (NCOLS - 34 - len(result.command))}")
        logger.debug(f"{mark} commandline: {shlex.join(result.commandline)}")
        logger.info(mark)
        logger.info(result.output)
        logger.info(mark)
        logger.info(f"{mark} returncode: {result.returncode}")
        logger.info(mark * NCOLS)
