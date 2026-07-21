"""prettierの2段階実行。"""

import argparse
import pathlib
import time
import typing

import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.config.config
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests
from pyfltr.command.two_step.base import _prepare_check_write_execution, _run_fix_mode


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
    cwd: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
) -> CommandResult:
    """Prettierの2段階実行（prettier --check → prettier --write）。

    `prettier --check`（read-only）と`prettier --write`（書き込み）は排他のため、
    既存のautoflake/isort/blackの「同じ引数に--checkを付与する」ダンスは適用できない。

    `{command}-args` と `{command}-extend-args` の結合は `resolve_user_args` で集約する。

    通常モード（fix_mode=False）:

    - Step1: `prefix + args + extend-args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded（書き込み不要）
    - Step1 rc == 1 → Step2 `prefix + args + extend-args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted（書き込み成功）
      - Step2 rc != 0 → failed
    - Step1 rc >= 2 → failed（設定ミス等）

    fixモード（fix_mode=True）:

    - Step1はスキップし、直接`prefix + args + extend-args + write-args + additional + targets`を実行
    - 書き込み検知には内容ハッシュスナップショットを使う
    - rc != 0 → failed
    - rc == 0かつハッシュ変化あり → formatted
    - rc == 0かつ変化なし → succeeded

    `commandline_prefix` はrunner解決済みの実行プレフィックス（例: `["ruff"]`、
    `["uv", "run", "--frozen", "ruff"]`、`["mise", "exec", "--", "ruff"]`）を呼び出し側から渡す。
    Python系ツールの `{command}-path` 既定値は空文字列のため、`config["<tool>-path"]` を
    直接参照する旧実装は動作しなくなる。同種関数を追加する際は本引数経由でプレフィックスを受け取る形を踏襲する。
    """
    # taplo/shfmt向けのbase.execute_check_write_two_stepと本関数は、分岐先ヘルパー
    # （_run_check_then_write / _run_prettier_check_then_write）が異なる別実装のため
    # 統合できないが、_prepare_check_write_executionへの引数受け渡し部分は完全一致する。
    # pylint: disable=duplicate-code
    check_commandline, write_commandline, run_step = _prepare_check_write_execution(
        command,
        commandline_prefix,
        config,
        targets,
        additional_args,
        env,
        on_output,
        args,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
        cwd=cwd,
        start_cwd=start_cwd,
    )
    # pylint: enable=duplicate-code
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
            run_step=run_step,
            start_time=start_time,
            parse_errors=True,
            command_type_override=_prettier_type_override,
            start_cwd=start_cwd,
        )

    return _run_prettier_check_then_write(
        command=command,
        command_info=command_info,
        check_commandline=check_commandline,
        write_commandline=write_commandline,
        targets=targets,
        run_step=run_step,
        start_time=start_time,
        start_cwd=start_cwd,
    )


def _run_prettier_check_then_write(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    check_commandline: list[str],
    write_commandline: list[str],
    targets: list[pathlib.Path],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
    start_time: float,
    *,
    start_cwd: pathlib.Path | None = None,
) -> CommandResult:
    """prettier専用の通常モード処理。

    Step1（check）rc==0→早期返却、rc>=2→即fail、rc==1→Step2（write）の順で処理する。
    taplo/shfmtと異なり、rc>=2の即fail判定とerror_parserの呼び出しが固有のロジックとして加わる。
    `run_step`は`_prepare_check_write_execution`が組み立てたcallableで、env・on_output・
    timeout・retry設定を既に束縛済み（commandlineのみ差し替えて呼び出す）。
    """
    step1_proc = run_step(check_commandline)
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
            retry_count=step1_proc.retry_count,
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
            retry_count=step1_proc.retry_count,
        )

    # Step1 rc == 1 → Step2実行（書き込み）
    prettier_digests_before = snapshot_file_digests(targets, base_cwd=start_cwd)
    step2_proc = run_step(write_commandline)
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
        retry_count=step1_proc.retry_count + step2_proc.retry_count,
    )
    if not has_error:
        prettier_digests_after = snapshot_file_digests(targets, base_cwd=start_cwd)
        changed = prettier_digests_after != prettier_digests_before
        if changed:
            result.fixed_files = changed_files(prettier_digests_before, prettier_digests_after)
    return result
