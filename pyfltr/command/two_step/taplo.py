# pylint: disable=duplicate-code,protected-access
"""taploの2段階実行。"""

import argparse
import pathlib
import shlex
import time
import typing

import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core import CommandResult
from pyfltr.command.snapshot import _changed_files, _snapshot_file_digests

logger = __import__("logging").getLogger(__name__)


def _execute_taplo_two_step(
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
    """Taploの2段階実行 （taplo check → taplo format）。

    shfmtと同様、確認用サブコマンド （check） と書き込み用サブコマンド （format） が
    排他のため専用経路で処理する。

    通常モード （fix_mode=False）:

    - Step1: `prefix + args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded （整形不要）
    - Step1 rc != 0 → Step2 `prefix + args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted （整形成功）
      - Step2 rc != 0 → failed

    fixモード （fix_mode=True）:

    - Step1をスキップし、直接write-args付きで実行
    -内容ハッシュスナップショットで書き込みを検知
    """
    common_args: list[str] = list(config[f"{command}-args"])
    check_args: list[str] = list(config[f"{command}-check-args"])
    write_args: list[str] = list(config[f"{command}-write-args"])
    target_strs = [str(t) for t in targets]

    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]

    if fix_mode:
        digests_before = _snapshot_file_digests(targets)
        if args.verbose and on_output is not None:
            on_output(f"commandline: {shlex.join(write_commandline)}\n")
        write_proc = pyfltr.command.process._run_subprocess(
            write_commandline,
            env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
        write_rc = write_proc.returncode
        output = write_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        digests_after = _snapshot_file_digests(targets)
        changed = digests_after != digests_before

        if write_rc != 0:
            has_error = True
            returncode: int = write_rc
        elif changed:
            has_error = False
            returncode = 1
        else:
            has_error = False
            returncode = 0

        result = CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=write_commandline,
            returncode=returncode,
            has_error=has_error,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )
        if not has_error and changed:
            result.fixed_files = _changed_files(digests_before, digests_after)
        return result

    # 通常モード: Step1 （check） → Step2 (format)
    # Step1はread-onlyのため内容変化なし。変化検知のためStep1前にスナップショットを取る。
    # （他formatterのdigests_beforeと同じ起点で取る方針に揃える）
    taplo_digests_before = _snapshot_file_digests(targets)
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    check_proc = pyfltr.command.process._run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    check_rc = check_proc.returncode

    if check_rc == 0:
        # 整形不要
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )

    # Step2: 書き込み
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    write_proc = pyfltr.command.process._run_subprocess(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    has_error = write_proc.returncode != 0
    returncode = write_proc.returncode if has_error else 1

    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=check_proc.stdout.strip() if not has_error else output,
        elapsed=elapsed,
    )
    if not has_error:
        taplo_digests_after = _snapshot_file_digests(targets)
        changed = taplo_digests_after != taplo_digests_before
        if changed:
            result.fixed_files = _changed_files(taplo_digests_before, taplo_digests_after)
    return result
