import dataclasses
import pathlib
import shlex
import tempfile
import time
import typing

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.command.slow_tests
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command.core_ import CommandResult
from pyfltr.command.invocation import build_invocation_argv

logger = __import__("logging").getLogger(__name__)


def _has_user_reporter_override(args_list: typing.Iterable[str]) -> bool:
    for arg in args_list:
        if arg == "--reporter" or arg.startswith("--reporter="):
            return True
        if arg == "--outputFile" or arg.startswith("--outputFile="):
            return True
        if arg.startswith("--outputFile."):
            return True
    return False


def execute_vitest(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    user_args: list[str] = list(pyfltr.config.model.command_setting(request.ctx.config.values, request.command, "args", []))
    if _has_user_reporter_override(user_args) or _has_user_reporter_override(request.params.additional_args):
        return _run_vitest_subprocess(request, commandline=request.params.commandline, json_output_path=None)

    with tempfile.NamedTemporaryFile(prefix="pyfltr-vitest-", suffix=".json", delete=False) as tmp:
        json_path = pathlib.Path(tmp.name)
    try:
        injection_args = [
            "--reporter=default",
            "--reporter=json",
            f"--outputFile.json={json_path}",
        ]
        argv = build_invocation_argv(
            request.command,
            request.ctx.config,
            request.params.commandline_prefix,
            request.params.additional_args,
            fix_stage=False,
        )
        argv.extend(injection_args)
        if pyfltr.config.model.command_setting(request.ctx.config.values, request.command, "pass-filenames", True):
            argv.extend(str(t) for t in request.params.targets)
        return _run_vitest_subprocess(request, commandline=argv, json_output_path=json_path)
    finally:
        json_path.unlink(missing_ok=True)


def _run_vitest_subprocess(
    request: pyfltr.command.core_.ExecutionRequest, *, commandline: list[str], json_output_path: pathlib.Path | None
) -> CommandResult:
    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(commandline)}\n")
    proc = pyfltr.command.process.run_process(dataclasses.replace(request, verbose=False), commandline)
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    parse_source = output
    if json_output_path is not None:
        try:
            parse_source = json_output_path.read_text(encoding="utf-8")
        except OSError:
            parse_source = output

    errors = request.parse_errors(parse_source, request.params.command_info.error_pattern)
    slow_tests = pyfltr.command.slow_tests.parse_vitest_durations(
        parse_source, base_cwd=(request.nodeid_base_cwd or request.ctx.effective_cwd)
    )
    result = CommandResult.from_process(
        process=proc,
        command=request.command,
        command_info=request.params.command_info,
        commandline=commandline,
        output=output,
        elapsed=elapsed,
        files=len(request.params.targets),
        errors=errors,
        slow_tests=slow_tests,
    )
    return result
