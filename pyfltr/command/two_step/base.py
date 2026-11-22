import functools
import pathlib
import time
import typing

import pyfltr.command.core_
import pyfltr.command.invocation
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.tools
from pyfltr.command import step_result
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests


def execute_check_write_two_step(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    check_commandline, write_commandline, run_step = _prepare_check_write_execution(request)
    if request.params.fix_mode:
        return _run_fix_mode(request, write_commandline=write_commandline, run_step=run_step, parse_errors=False)

    return _run_check_then_write(
        request, check_commandline=check_commandline, write_commandline=write_commandline, run_step=run_step
    )


def _relative_to_cwd(target: pathlib.Path, *, cwd: pathlib.Path, start_cwd: pathlib.Path) -> str:
    abs_path = target if target.is_absolute() else (start_cwd / target)
    try:
        rel = abs_path.resolve().relative_to(cwd.resolve())
    except (OSError, ValueError):
        return pyfltr.paths.normalize_separators(target)
    return pyfltr.paths.normalize_separators(rel)


def _prepare_check_write_execution(
    request: pyfltr.command.core_.ExecutionRequest,
) -> tuple[list[str], list[str], typing.Callable[[list[str]], pyfltr.command.process.CompletedProcessWithTimeoutInfo]]:
    common_args = pyfltr.command.invocation.resolve_user_args(request.command, request.ctx.config)
    if request.cwd is not None:
        external_targets = [
            _relative_to_cwd(t, cwd=request.cwd, start_cwd=request.ctx.base.start_cwd) for t in request.params.targets
        ]
    else:
        external_targets = [str(t) for t in request.params.targets]
    check_commandline, write_commandline = _build_commandlines(
        request.params.commandline_prefix,
        common_args,
        pyfltr.command.invocation.expanduser_args(
            list(pyfltr.config.model.required_command_setting(request.ctx.config.values, request.command, "check-args"))
        ),
        pyfltr.command.invocation.expanduser_args(
            list(pyfltr.config.model.required_command_setting(request.ctx.config.values, request.command, "write-args"))
        ),
        request.params.additional_args,
        external_targets,
    )
    return check_commandline, write_commandline, functools.partial(pyfltr.command.process.run_process, request)


def _run_check_then_write(
    request: pyfltr.command.core_.ExecutionRequest,
    *,
    check_commandline: list[str],
    write_commandline: list[str],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
) -> CommandResult:
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    check_proc = run_step(check_commandline)
    check_rc = check_proc.returncode
    checked = step_result.classify(check_rc, timeout=check_proc.timeout_exceeded, change_codes=range(1, max(2, check_rc + 1)))

    if checked.state is step_result.StepState.SUCCEEDED:
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - request.start_time
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(request.params.targets),
            output=output,
            elapsed=elapsed,
            timeout_exceeded=check_proc.timeout_exceeded,
            retry_count=check_proc.retry_count,
        )

    if checked.failed:
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - request.start_time
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=check_commandline,
            returncode=checked.returncode,
            formatter_failed=True,
            files=len(request.params.targets),
            output=output,
            elapsed=elapsed,
            timeout_exceeded=check_proc.timeout_exceeded,
            retry_count=check_proc.retry_count,
        )

    write_proc = run_step(write_commandline)
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    combined = step_result.combine(checked, step_result.classify(write_proc.returncode, timeout=write_proc.timeout_exceeded))
    formatter_failed = combined.failed
    returncode = combined.returncode

    result = CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=write_commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=check_proc.stdout.strip() if not formatter_failed else output,
        elapsed=elapsed,
        timeout_exceeded=write_proc.timeout_exceeded,
        retry_count=check_proc.retry_count + write_proc.retry_count,
    )
    if not formatter_failed:
        digests_after = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
        changed = digests_after != digests_before
        if changed:
            result.fixed_files = changed_files(digests_before, digests_after)
    return result


def _run_fix_mode(
    request: pyfltr.command.core_.ExecutionRequest,
    *,
    write_commandline: list[str],
    run_step: typing.Callable[[list[str]], "pyfltr.command.process.CompletedProcessWithTimeoutInfo"],
    parse_errors: bool,
    command_type_override: typing.Callable[[bool, int], str] | None = None,
) -> CommandResult:
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    write_proc = run_step(write_commandline)
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time
    digests_after = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    changed = digests_after != digests_before

    combined = step_result.combine(
        step_result.classify(write_proc.returncode, timeout=write_proc.timeout_exceeded, changed=changed)
    )
    formatter_failed = combined.failed
    returncode = combined.returncode

    errors = request.parse_errors(output, request.params.command_info.error_pattern) if parse_errors else []

    resolved_type = (
        command_type_override(formatter_failed, returncode)
        if command_type_override is not None
        else request.params.command_info.type
    )
    result = CommandResult.from_run(
        command=request.command,
        command_type=resolved_type,
        commandline=write_commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=write_proc.timeout_exceeded,
        retry_count=write_proc.retry_count,
    )
    if not formatter_failed and changed:
        result.fixed_files = changed_files(digests_before, digests_after)
    return result


def _build_commandlines(
    commandline_prefix: list[str],
    common_args: list[str],
    check_args: list[str],
    write_args: list[str],
    additional_args: list[str],
    target_strs: list[str],
) -> tuple[list[str], list[str]]:
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]
    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]
    return check_commandline, write_commandline
