# pylint: disable=duplicate-code,protected-access
"""prettierの2段階実行。"""

import argparse
import pathlib
import shlex
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core import CommandResult
from pyfltr.command.snapshot import _changed_files, _snapshot_file_digests

logger = __import__("logging").getLogger(__name__)


def _execute_prettier_two_step(
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
    """Prettierの2段階実行 （prettier --check → prettier --write）。

    `prettier --check` （read-only） と `prettier --write` （書き込み） は排他のため、
    既存のautoflake/isort/blackの「同じ引数に--checkを付与する」ダンスは使えない。
    本ヘルパーでは以下のとおり実行する。

    通常モード （fix_mode=False）:

    - Step1: `prefix + args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded （書き込み不要）
    - Step1 rc == 1 → Step2 `prefix + args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted （書き込み成功）
      - Step2 rc != 0 → failed
    - Step1 rc >= 2 → failed （設定ミス等）

    fixモード （fix_mode=True）:

    - Step1はスキップし、直接 `prefix + args + write-args + additional + targets`を実行
    -書き込み検知には内容ハッシュスナップショットを使う
    - rc != 0 → failed
    - rc == 0かつハッシュ変化あり → formatted
    - rc == 0かつ変化なし → succeeded
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
            result_command_type: str = command_info.type
        elif changed:
            has_error = False
            returncode = 1
            result_command_type = "formatter"
        else:
            has_error = False
            returncode = 0
            result_command_type = command_info.type

        errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)
        result = CommandResult.from_run(
            command=command,
            command_type=result_command_type,
            commandline=write_commandline,
            returncode=returncode,
            has_error=has_error,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )
        if not has_error and changed:
            result.fixed_files = _changed_files(digests_before, digests_after)
        return result

    # 通常モード: Step1 （check） → 必要ならStep2 （write）
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = pyfltr.command.process._run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode

    if step1_rc == 0:
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )

    if step1_rc >= 2:
        # 設定ミス等の致命的エラー。Step2は実行しない。
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=step1_rc,
            has_error=True,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )

    # Step1 rc == 1 → Step2実行 （書き込み）
    prettier_digests_before = _snapshot_file_digests(targets)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    step2_proc = pyfltr.command.process._run_subprocess(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step2_rc = step2_proc.returncode
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    if step2_rc == 0:
        has_error = False
        returncode = 1  # formatted扱い
    else:
        has_error = True
        returncode = step2_rc

    errors = pyfltr.command.error_parser.parse_errors(command, output, command_info.error_pattern)
    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )
    if not has_error:
        prettier_digests_after = _snapshot_file_digests(targets)
        changed = prettier_digests_after != prettier_digests_before
        if changed:
            result.fixed_files = _changed_files(prettier_digests_before, prettier_digests_after)
    return result
