"""コマンドライン処理。"""

import argparse
import logging
import shlex
import threading
import typing

import joblib

import pyfltr.command
import pyfltr.config

logger = logging.getLogger(__name__)

NCOLS = 128
lock = threading.Lock()


def run_commands_with_cli(commands: list[str], args: argparse.Namespace) -> list[pyfltr.command.CommandResult]:
    """コマンドの実行。"""
    results: list[pyfltr.command.CommandResult] = []

    # run formatters (serial)
    for command in commands:
        if pyfltr.config.CONFIG[command] and pyfltr.config.ALL_COMMANDS[command].type == "formatter":
            results.append(run_command_for_cli(command, args))

    # run linters/testers (parallel)
    jobs: list[typing.Any] = []
    for command in commands:
        if pyfltr.config.CONFIG[command] and pyfltr.config.ALL_COMMANDS[command].type != "formatter":
            jobs.append(joblib.delayed(run_command_for_cli)(command, args))
    if len(jobs) > 0:
        with joblib.Parallel(n_jobs=len(jobs), backend="threading") as parallel:
            results.extend(parallel(jobs))

    return results


def run_command_for_cli(command: str, args: argparse.Namespace) -> pyfltr.command.CommandResult:
    """コマンドの実行（コンソール表示）。"""
    result = pyfltr.command.execute_command(command, args)
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
