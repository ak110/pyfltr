"""taploの2段階実行。"""

import argparse
import pathlib
import typing

import pyfltr.config.config
from pyfltr.command.core_ import CommandResult
from pyfltr.command.two_step.base import execute_check_write_two_step


def execute_taplo_two_step(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_mode: bool,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Taploの2段階実行（taplo check → taplo format）。

    checkとformatが排他のサブコマンド構成を持つツール向け。
    `execute_check_write_two_step` の薄いラッパーとして実装する。
    """
    return execute_check_write_two_step(
        command=command,
        command_info=command_info,
        commandline_prefix=commandline_prefix,
        config=config,
        targets=targets,
        additional_args=additional_args,
        fix_mode=fix_mode,
        env=env,
        on_output=on_output,
        start_time=start_time,
        args=args,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
