#!/usr/bin/env python3
"""pyfltr。"""

import argparse
import importlib.metadata
import logging
import os
import pathlib
import sys
import typing

import pyfltr.cli
import pyfltr.command
import pyfltr.config
import pyfltr.ui

logger = logging.getLogger(__name__)


def main() -> typing.NoReturn:
    """エントリポイント。"""
    exit_code = run()
    logger.debug(f"{exit_code=}")
    sys.exit(exit_code)


def run(sys_args: typing.Sequence[str] | None = None) -> int:
    """処理の実行。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", default=False, action="store_true", help="shows verbose output.")
    parser.add_argument(
        "--exit-zero-even-if-formatted",
        default=False,
        action="store_true",
        help="exit 1 only if linters/testers has errors.",
    )
    parser.add_argument(
        "--commands",
        default=",".join(pyfltr.config.ALL_COMMAND_NAMES),
        help="comma separated list of commands. (default: %(default)s)",
    )
    parser.add_argument(
        "--generate-config",
        default=False,
        action="store_true",
        help="generate a sample configuration. (part of pyproject.toml)",
    )
    parser.add_argument("--ui", default=None, action="store_true", help="force enable textual UI")
    parser.add_argument("--no-ui", default=None, action="store_true", help="force disable textual UI")
    parser.add_argument("--shuffle", default=False, action="store_true", help="shuffle file order")
    parser.add_argument("--ci", default=False, action="store_true", help="CI mode (equivalent to --no-shuffle --no-ui)")

    # 各コマンド用の引数追加オプション
    for command in pyfltr.config.ALL_COMMANDS:
        parser.add_argument(
            f"--{command}-args",
            default="",
            help=f"additional arguments for {command}",
        )

    parser.add_argument(
        "targets",
        nargs="*",
        type=pathlib.Path,
        help="target files and/or directories. (default: .)",
    )
    parser.add_argument("--version", "-V", action="store_true", help="show version")
    args = parser.parse_args(sys_args)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    # --ciオプションの処理
    if args.ci:
        args.shuffle = False
        args.no_ui = True

    # --ui と --no-ui の競合チェック
    if args.ui and args.no_ui:
        parser.error("--ui and --no-ui cannot be used together")

    # --version
    if args.version:
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    # --generate-config
    if args.generate_config:
        logger.info(pyfltr.config.generate_config_text())
        return 0

    # 実行環境の情報を出力
    logger.info(f"{'-' * 10} pyfltr {'-' * (72 - 10 - 8)}")
    logger.info(f"version:        {importlib.metadata.version('pyfltr')}")
    logger.info(f"sys.executable: {sys.executable}")
    logger.info(f"sys.version:    {sys.version}")
    logger.info(f"cwd:            {os.getcwd()}")
    logger.info("-" * 72)

    # check
    commands: list[str] = pyfltr.config.resolve_aliases(args.commands.split(","))
    for command in commands:
        if command not in pyfltr.config.CONFIG:
            parser.error(f"command not found: {command}")

    # pyproject.toml
    try:
        pyfltr.config.load_config()
    except (ValueError, OSError) as e:
        logger.error(f"Config error: {e}")
        return 1

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.ui.can_use_ui())

    # run
    if use_ui:
        results, returncode = pyfltr.ui.run_commands_with_ui(commands, args)
        # UI終了後に通常のログを出力
        for result in results:
            pyfltr.cli.write_log(result)
    else:
        results = pyfltr.cli.run_commands_with_cli(commands, args)
        returncode = 0

    # summary

    logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
    for result in sorted(results, key=lambda r: pyfltr.config.ALL_COMMAND_NAMES.index(r.command)):
        logger.info(f"    {result.command:<16s} {result.get_status_text()}")
    logger.info("-" * 72)

    # returncode
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)
    return returncode


def calculate_returncode(results: list[pyfltr.command.CommandResult], exit_zero_even_if_formatted: bool) -> int:
    """終了コードを計算。"""
    statuses = [result.status for result in results]
    if any(status == "failed" for status in statuses):
        return 1
    if not exit_zero_even_if_formatted and any(status == "formatted" for status in statuses):
        return 1
    return 0


if __name__ == "__main__":
    main()
