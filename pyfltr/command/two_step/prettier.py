import time
import typing

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command import step_result
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests
from pyfltr.command.two_step.base import _prepare_check_write_execution, _run_fix_mode


def execute_prettier_two_step(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    check_commandline, write_commandline, run_step = _prepare_check_write_execution(request)
    if request.params.fix_mode:

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
    step1_proc = run_step(check_commandline)
    step1_rc = step1_proc.returncode
    checked = step_result.classify(step1_rc, timeout=step1_proc.timeout_exceeded, change_codes=(1,))

    if checked.state is step_result.StepState.SUCCEEDED:
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - request.start_time
        errors = request.parse_errors(output, request.params.command_info.error_pattern)
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

    if checked.failed:
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - request.start_time
        errors = request.parse_errors(output, request.params.command_info.error_pattern)
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=check_commandline,
            returncode=checked.returncode,
            formatter_failed=True,
            files=len(request.params.targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
            timeout_exceeded=step1_proc.timeout_exceeded,
            retry_count=step1_proc.retry_count,
        )

    prettier_digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    step2_proc = run_step(write_commandline)
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - request.start_time

    combined = step_result.combine(checked, step_result.classify(step2_proc.returncode, timeout=step2_proc.timeout_exceeded))
    formatter_failed = combined.failed
    returncode = combined.returncode

    errors = request.parse_errors(output, request.params.command_info.error_pattern)
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
