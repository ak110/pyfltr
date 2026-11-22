import time

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
from pyfltr.command import step_result
from pyfltr.command.core_ import CommandResult
from pyfltr.command.snapshot import changed_files, snapshot_file_digests

logger = __import__("logging").getLogger(__name__)


def execute_linter_fix(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    digests_before = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)

    proc = pyfltr.command.process.run_process(request)
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    digests_after = snapshot_file_digests(request.params.targets, base_cwd=request.ctx.base.start_cwd)
    changed = digests_after != digests_before

    combined = step_result.combine(step_result.classify(proc.returncode, timeout=proc.timeout_exceeded, changed=changed))
    step_failed = combined.failed
    returncode = combined.returncode
    result_command_type = "formatter" if combined.state is step_result.StepState.CHANGED else "linter"

    errors = request.parse_errors(output, None)

    result = CommandResult.from_run(
        command=request.command,
        command_type=result_command_type,
        commandline=request.params.commandline,
        returncode=returncode,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=proc.timeout_exceeded,
        retry_count=proc.retry_count,
    )
    if not step_failed and changed:
        result.fixed_files = changed_files(digests_before, digests_after)
    return result
