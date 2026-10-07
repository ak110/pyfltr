"""専用実行のテスト入力から実行要求を作成する。"""

import argparse
import pathlib
import typing

import pyfltr.command.core_
import pyfltr.config.model
import pyfltr.run_options
import pyfltr.tools


def make_request(
    command: str,
    command_info: pyfltr.tools.CommandInfo,
    config: pyfltr.config.model.Config,
    env: dict[str, str] | None,
    targets: list[pathlib.Path],
    start_time: float,
    args: argparse.Namespace | pyfltr.run_options.RunOptions,
    commandline: list[str] | None = None,
    format_commandline: list[str] | None = None,
    commandline_prefix: list[str] | None = None,
    cache_commandline: list[str] | None = None,
    additional_args: list[str] | None = None,
    fix_mode: bool = False,
    fix_args: list[str] | None = None,
    on_output: typing.Callable[[str], None] | None = None,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
    cwd: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
    nodeid_base_cwd: pathlib.Path | None = None,
    cache_store: typing.Any = None,
    cache_run_id: str | None = None,
    subproject_cwd: pathlib.Path | None = None,
    injected_config_path: pathlib.Path | None = None,
    file_path_remap: dict[str, str] | None = None,
) -> pyfltr.command.core_.ExecutionRequest:
    """実行設定と所有コールバックを既存の入力どおりに束ねる。"""
    params = pyfltr.command.core_.ExecutionParams(
        command_info=command_info,
        targets=targets,
        commandline_prefix=commandline_prefix or [],
        commandline=commandline or format_commandline or [],
        cache_commandline=cache_commandline or commandline or [],
        additional_args=additional_args or [],
        fix_mode=fix_mode,
        fix_args=fix_args,
        injected_config_path=injected_config_path,
        file_path_remap=file_path_remap,
    )
    ctx = pyfltr.command.core_.ExecutionContext(
        base=pyfltr.command.core_.ExecutionBaseContext(
            config=config,
            all_files=targets,
            cache_store=cache_store,
            cache_run_id=cache_run_id,
            start_cwd=start_cwd or cwd or pathlib.Path.cwd(),
        ),
        fix_stage=fix_mode,
        subproject_cwd=subproject_cwd,
        on_output=on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    return pyfltr.command.core_.ExecutionRequest(
        command=command,
        params=params,
        ctx=ctx,
        env=typing.cast(dict[str, str], env),
        start_time=start_time,
        verbose=args.verbose,
        cwd=cwd,
        nodeid_base_cwd=nodeid_base_cwd,
    )
