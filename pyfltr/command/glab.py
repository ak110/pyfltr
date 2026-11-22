import dataclasses
import re
import shlex
import time

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
import pyfltr.warnings_
from pyfltr.command.core_ import CommandResult

logger = __import__("logging").getLogger(__name__)


_GLAB_HOST_NOT_FOUND_PATTERNS: tuple[str, ...] = (
    "none of the git remotes configured for this repository point to a known gitlab host",
    "not authenticated",
)


def _looks_like_glab_host_missing(output: str) -> bool:
    normalized = re.sub(r"\s+", " ", output.lower())
    return any(pattern in normalized for pattern in _GLAB_HOST_NOT_FOUND_PATTERNS)


def execute_glab_ci_lint(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    glab_env = dict(request.env)
    glab_env["LC_ALL"] = "C"
    glab_env["LANG"] = "C"

    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(request.params.commandline)}\n")

    proc = pyfltr.command.process.run_process(
        dataclasses.replace(request, env=glab_env, verbose=False), request.params.commandline
    )
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - request.start_time

    if returncode != 0 and _looks_like_glab_host_missing(output):
        message = "glab がGitLabホストを検出できなかったためスキップしました。"
        hint = (
            "環境変数 `GITLAB_HOST` を設定するか `glab auth login` を実行してください。"
            f"GitLab CIの検査が不要なら `{request.command} = false` で無効化してください。"
        )
        pyfltr.warnings_.emit_warning(source=request.command, message=message, hint=hint)
        skip_text = f"{message}{hint}"
        skip_output = f"{skip_text}\n\n{output}" if output else skip_text
        return CommandResult.from_run(
            command=request.command,
            command_info=request.params.command_info,
            commandline=request.params.commandline,
            returncode=None,
            output=skip_output,
            files=len(request.params.targets),
            elapsed=elapsed,
        )

    errors = request.parse_errors(output, request.params.command_info.error_pattern)
    result = CommandResult.from_run(
        command=request.command,
        command_info=request.params.command_info,
        commandline=request.params.commandline,
        returncode=returncode,
        output=output,
        elapsed=elapsed,
        files=len(request.params.targets),
        errors=errors,
        timeout_exceeded=proc.timeout_exceeded,
        retry_count=proc.retry_count,
    )
    return result
