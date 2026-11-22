import dataclasses
import logging
import os
import pathlib
import shlex
import time
import typing

import pyfltr.command.core_
import pyfltr.command.precommit_guidance
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command import step_result
from pyfltr.command.core_ import CommandResult

logger = logging.getLogger(__name__)


def execute_pre_commit(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    if pyfltr.command.precommit_guidance.is_running_under_precommit():
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=request.params.commandline,
            returncode=None,
            output=f"pre-commit・prek配下で実行されたため{request.command}統合をスキップしました。二重実行を回避したため対応不要です。",
            files=len(request.params.targets),
            elapsed=time.perf_counter() - request.start_time,
        )

    config_dir = request.cwd if request.cwd is not None else pathlib.Path.cwd()
    config_path = config_dir / ".pre-commit-config.yaml"
    if not config_path.exists():
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=request.params.commandline,
            returncode=None,
            output=(
                f".pre-commit-config.yaml が見つからないため{request.command}統合をスキップしました。"
                "フックを使う場合は .pre-commit-config.yaml を作成し、"
                f"使わない場合は `{request.command} = false` で無効化してください。"
            ),
            files=len(request.params.targets),
            elapsed=time.perf_counter() - request.start_time,
        )

    integration_command = typing.cast(typing.Literal["pre-commit", "prek"], request.command)
    skip_value = pyfltr.command.precommit_guidance.build_skip_value(request.ctx.config, config_dir, integration_command)
    pre_commit_env = dict(request.env) if request.env is not None else dict(os.environ)
    if skip_value:
        existing_skip = pre_commit_env.get("SKIP", "")
        if existing_skip:
            pre_commit_env["SKIP"] = f"{existing_skip},{skip_value}"
        else:
            pre_commit_env["SKIP"] = skip_value

    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(request.params.commandline)}\n")
        if skip_value:
            request.ctx.on_output(f"SKIP={pre_commit_env.get('SKIP', '')}\n")

    def _run_stage() -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.run_process(
            dataclasses.replace(request, verbose=False, env=pre_commit_env), request.params.commandline
        )

    proc = _run_stage()
    returncode = proc.returncode
    timeout_exceeded = proc.timeout_exceeded
    total_retry_count = proc.retry_count
    first = step_result.classify(returncode, timeout=timeout_exceeded, change_codes=range(1, max(2, returncode + 1)))
    combined = step_result.combine(first)

    if first.state is step_result.StepState.CHANGED:
        if request.verbose and request.ctx.on_output is not None:
            request.ctx.on_output(f"{request.command}: stage 2 再実行\n")
        proc = _run_stage()
        combined = step_result.combine(first, step_result.classify(proc.returncode, timeout=proc.timeout_exceeded))
        if proc.timeout_exceeded:
            timeout_exceeded = True
        total_retry_count += proc.retry_count

    returncode = combined.returncode
    formatter_failed = combined.failed

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    return CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=request.params.commandline,
        returncode=returncode,
        formatter_failed=formatter_failed,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        timeout_exceeded=timeout_exceeded,
        retry_count=total_retry_count,
    )
