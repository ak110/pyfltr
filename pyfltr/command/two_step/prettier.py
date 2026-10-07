"""prettierの2段階実行。"""

import time
import typing

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.parsing.entry
import pyfltr.tools
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests
from pyfltr.command.two_step.base import _prepare_check_write_execution, _run_fix_mode


def execute_prettier_two_step(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
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
    check_commandline, write_commandline, run_step = _prepare_check_write_execution(request)
    if request.params.fix_mode:
        # fixモードのみ: returncode==1（changed）のときcommand_typeを"formatter"に切り替える。
        # 通常モードのcommand_infoから取得する型がformatter以外の場合に備えた固有ロジック。
        def _prettier_type_override(formatter_failed: bool, returncode: int) -> str:
            if not formatter_failed and returncode == 1:
                return "formatter"
            return request.params.command_info.type

        return _run_fix_mode(
            request,
            write_commandline=write_commandline,
            run_step=run_step,
            parse_errors=True,
            command_type_override=_prettier_type_override,
        )

    return _run_prettier_check_then_write(
        request, check_commandline=check_commandline, write_commandline=write_commandline, run_step=run_step
    )


def _run_prettier_check_then_write(
    request: pyfltr.command.core_.ExecutionRequest,
    *,
    check_commandline: list[str],
    write_commandline: list[str],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
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
        elapsed = time.perf_counter() - request.start_time
        errors = pyfltr.parsing.entry.parse_errors(request.command, output, request.params.command_info.error_pattern)
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(request.params.targets),
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
        elapsed = time.perf_counter() - request.start_time
        errors = pyfltr.parsing.entry.parse_errors(request.command, output, request.params.command_info.error_pattern)
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=check_commandline,
            returncode=step1_rc,
            formatter_failed=True,
            files=len(request.params.targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
            timeout_exceeded=step1_proc.timeout_exceeded,
            retry_count=step1_proc.retry_count,
        )

    # Step1 rc == 1 → Step2実行（書き込み）
    prettier_digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    step2_proc = run_step(write_commandline)
    step2_rc = step2_proc.returncode
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - request.start_time

    if step2_rc == 0:
        formatter_failed = False
        returncode: int = 1  # formatted扱い
    else:
        formatter_failed = True
        returncode = step2_rc

    errors = pyfltr.parsing.entry.parse_errors(request.command, output, request.params.command_info.error_pattern)
    result = CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=write_commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=step2_proc.timeout_exceeded,
        retry_count=step1_proc.retry_count + step2_proc.retry_count,
    )
    if not formatter_failed:
        prettier_digests_after = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
        changed = prettier_digests_after != prettier_digests_before
        if changed:
            result.fixed_files = changed_files(prettier_digests_before, prettier_digests_after)
    return result
