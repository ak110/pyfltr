import functools
import time

import pyfltr.command.core_
import pyfltr.command.invocation
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command import step_result
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests
from pyfltr.command.two_step.base import _relative_to_cwd


def execute_ruff_format_two_step(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    check_commandline: list[str] = list(request.params.commandline_prefix)
    check_commandline.extend(pyfltr.command.invocation.expanduser_args(list(request.ctx.config["ruff-format-check-args"])))
    if request.cwd is not None and request.ctx.base.start_cwd is not None:
        check_commandline.extend(
            _relative_to_cwd(t, cwd=request.cwd, start_cwd=request.ctx.base.start_cwd) for t in request.params.targets
        )
    else:
        check_commandline.extend(str(t) for t in request.params.targets)

    return _run_ruff_two_step(request, check_commandline=check_commandline)


def _run_ruff_two_step(request: pyfltr.command.core_.ExecutionRequest, *, check_commandline: list[str]) -> CommandResult:
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)

    run_step = functools.partial(pyfltr.command.process.run_process, request)
    step1_proc = run_step(check_commandline)
    digests_after_step1 = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    step1_changed = digests_after_step1 != digests_before

    step2_proc = run_step(request.params.commandline)
    step2_formatted = step2_proc.returncode == 1

    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - request.start_time

    timeout_exceeded = step1_proc.timeout_exceeded or step2_proc.timeout_exceeded
    combined = step_result.combine(
        step_result.classify(
            step1_proc.returncode, timeout=step1_proc.timeout_exceeded, success_codes=(0, 1), changed=step1_changed
        ),
        step_result.classify(step2_proc.returncode, timeout=step2_proc.timeout_exceeded, change_codes=(1,)),
    )
    formatter_failed = combined.failed
    returncode = combined.returncode

    errors = request.parse_errors(output, request.params.command_info.error_pattern)

    result = CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=request.params.commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=timeout_exceeded,
        retry_count=step1_proc.retry_count + step2_proc.retry_count,
    )
    if not formatter_failed and (step1_changed or step2_formatted):
        digests_after_step2 = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
        result.fixed_files = changed_files(digests_before, digests_after_step2)
    return result
