"""prettierの2段階実行。"""

import argparse
import pathlib
import shlex
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests
from pyfltr.command.two_step.base import _build_commandlines, _run_fix_mode


def execute_prettier_two_step(
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
    """Prettierの2段階実行（prettier --check → prettier --write）。

    `prettier --check`（read-only）と`prettier --write`（書き込み）は排他のため、
    既存のautoflake/isort/blackの「同じ引数に--checkを付与する」ダンスは適用できない。

    通常モード（fix_mode=False）:

    - Step1: `prefix + args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded（書き込み不要）
    - Step1 rc == 1 → Step2 `prefix + args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted（書き込み成功）
      - Step2 rc != 0 → failed
    - Step1 rc >= 2 → failed（設定ミス等）

    fixモード（fix_mode=True）:

    - Step1はスキップし、直接`prefix + args + write-args + additional + targets`を実行
    - 書き込み検知には内容ハッシュスナップショットを使う
    - rc != 0 → failed
    - rc == 0かつハッシュ変化あり → formatted
    - rc == 0かつ変化なし → succeeded

    `commandline_prefix` はrunner解決済みの実行プレフィックス（例: `["ruff"]`、
    `["uv", "run", "--frozen", "ruff"]`、`["mise", "exec", "--", "ruff"]`）を呼び出し側から渡す。
    Python系ツールの `{command}-path` 既定値は空文字列のため、`config["<tool>-path"]` を
    直接参照する旧実装は動作しなくなる。同種関数を追加する際は本引数経由でプレフィックスを受け取る形を踏襲する。
    """
    common_args: list[str] = pyfltr.command.runner.expanduser_args(list(config[f"{command}-args"]))
    check_commandline, write_commandline = _build_commandlines(
        commandline_prefix,
        common_args,
        pyfltr.command.runner.expanduser_args(list(config[f"{command}-check-args"])),
        pyfltr.command.runner.expanduser_args(list(config[f"{command}-write-args"])),
        additional_args,
        [str(t) for t in targets],
    )

    timeout = pyfltr.config.config.resolve_command_timeout(config.values, command)
    if fix_mode:
        # fixモードのみ: returncode==1（changed）のときcommand_typeを"formatter"に切り替える。
        # 通常モードのcommand_infoから取得する型がformatter以外の場合に備えた固有ロジック。
        def _prettier_type_override(has_error: bool, returncode: int) -> str:
            if not has_error and returncode == 1:
                return "formatter"
            return command_info.type

        return _run_fix_mode(
            command=command,
            command_info=command_info,
            write_commandline=write_commandline,
            targets=targets,
            env=env,
            args=args,
            start_time=start_time,
            timeout=timeout,
            parse_errors=True,
            command_type_override=_prettier_type_override,
            is_interrupted=is_interrupted,
            on_output=on_output,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )

    return _run_prettier_check_then_write(
        command=command,
        command_info=command_info,
        check_commandline=check_commandline,
        write_commandline=write_commandline,
        targets=targets,
        env=env,
        args=args,
        start_time=start_time,
        timeout=timeout,
        is_interrupted=is_interrupted,
        on_output=on_output,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )


def _run_prettier_check_then_write(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    check_commandline: list[str],
    write_commandline: list[str],
    targets: list[pathlib.Path],
    env: dict[str, str],
    args: argparse.Namespace,
    start_time: float,
    *,
    timeout: float | None,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_output: typing.Callable[[str], None] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """prettier専用の通常モード処理。

    Step1（check）rc==0→早期返却、rc>=2→即fail、rc==1→Step2（write）の順で処理する。
    taplo/shfmtと異なり、rc>=2の即fail判定とerror_parserの呼び出しが固有のロジックとして加わる。
    """
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = pyfltr.command.process.run_subprocess_with_timeout(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=timeout,
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
            timeout_exceeded=step1_proc.timeout_exceeded,
        )

    if step1_rc >= 2 or step1_proc.timeout_exceeded:
        # 設定ミス等の致命的エラー、もしくはcheck段でtimeout超過した場合はStep2をスキップする。
        # timeout超過は同じハングが再現する確率が高く、検証時間を浪費するためStep2を実行しない。
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
            timeout_exceeded=step1_proc.timeout_exceeded,
        )

    # Step1 rc == 1 → Step2実行（書き込み）
    prettier_digests_before = snapshot_file_digests(targets)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    step2_proc = pyfltr.command.process.run_subprocess_with_timeout(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        timeout=timeout,
    )
    step2_rc = step2_proc.returncode
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    if step2_rc == 0:
        has_error = False
        returncode: int = 1  # formatted扱い
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
        timeout_exceeded=step2_proc.timeout_exceeded,
    )
    if not has_error:
        prettier_digests_after = snapshot_file_digests(targets)
        changed = prettier_digests_after != prettier_digests_before
        if changed:
            result.fixed_files = changed_files(prettier_digests_before, prettier_digests_after)
    return result
