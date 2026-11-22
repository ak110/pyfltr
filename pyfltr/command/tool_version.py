import functools
import pathlib
import re

import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command.env import build_subprocess_env

PACKAGE_MANAGER_MIN_VERSION = {
    command: info.minimum_version for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.minimum_version is not None
}


_VERSION_TOKEN_PATTERN = re.compile(r"^v?(\d+(?:\.\d+)+)")

_VERSION_PROBE_TIMEOUT = 30.0


def _extract_tool_version(output: str) -> tuple[int, ...] | None:
    for line in output.splitlines():
        for token in line.split():
            matched = _VERSION_TOKEN_PATTERN.match(token)
            if matched is not None:
                return tuple(int(n) for n in matched.group(1).split("."))
    return None


def _format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)


@functools.lru_cache(maxsize=1)
def _get_tool_version(
    commandline: tuple[str, ...], env_items: tuple[tuple[str, str], ...], cwd: pathlib.Path | None
) -> tuple[int, ...] | None:
    proc = pyfltr.command.process.run_process_loop(
        [*commandline, "--version"], dict(env_items), cwd=cwd, timeout=_VERSION_PROBE_TIMEOUT
    )
    if proc.returncode != 0:
        return None
    return _extract_tool_version(proc.stdout)


def probe_tool_version_text(
    resolved: pyfltr.command.runner.ResolvedCommandline,
    config: pyfltr.config.model.Config,
    command: str,
    *,
    cwd: pathlib.Path | None = None,
) -> str | None:
    env = build_subprocess_env(
        config,
        command,
        via_mise=resolved.effective_runner == "mise",
        effective_runner=resolved.effective_runner,
    )
    try:
        proc = pyfltr.command.process.run_process_loop(
            [*resolved.commandline, "--version"], env, cwd=cwd, timeout=_VERSION_PROBE_TIMEOUT
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return _preferred_version_line(proc.stdout)


def _preferred_version_line(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.casefold().startswith("warning"):
            continue
        if any(_VERSION_TOKEN_PATTERN.match(token) is not None for token in stripped.split()):
            return stripped
    return None


def ensure_package_manager_version(
    resolved: pyfltr.command.runner.ResolvedCommandline,
    config: pyfltr.config.model.Config,
    command: str,
    *,
    cwd: pathlib.Path | None = None,
) -> None:
    requirement = PACKAGE_MANAGER_MIN_VERSION.get(command)
    if requirement is None:
        return
    minimum, reason = requirement
    minimum_text = _format_version(minimum)
    env = build_subprocess_env(config, command, effective_runner=resolved.effective_runner)
    version = _get_tool_version(tuple(resolved.commandline), tuple(sorted(env.items())), cwd)
    shown = " ".join(resolved.commandline)
    hint = f"`{command}-path`で要件を満たす実行ファイルを指定することもできます。"
    if version is None:
        raise ValueError(
            f"{shown}のバージョンを判別できませんでした。{command}には{minimum_text}以降が必要です。{reason}。{hint}"
        )
    if version < minimum:
        version_text = _format_version(version)
        raise ValueError(
            f"{shown}のバージョンは{version_text}です。{command}には{minimum_text}以降が必要です。{reason}。{hint}"
        )
