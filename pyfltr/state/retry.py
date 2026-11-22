import logging
import os
import pathlib
import shlex
import sys

import pyfltr.command.core_
import pyfltr.paths

logger = logging.getLogger(__name__)

_VALUE_OPTIONS: frozenset[str] = frozenset(
    {
        "--commands",
        "--from-run",
        "--output-format",
        "--output-file",
        "--work-dir",
        "-j",
        "--jobs",
    }
)


def detect_launcher_prefix() -> list[str]:
    fallback = [os.path.basename(sys.argv[0]) or "pyfltr"]
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            ppid_line = next((line for line in f if line.startswith("PPid:")), None)
        if ppid_line is None:
            return fallback
        ppid = ppid_line.split(":", 1)[1].strip()
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return fallback
    tokens = [tok.decode("utf-8", errors="replace") for tok in raw.split(b"\0") if tok]
    if not tokens:
        return fallback
    launcher = os.path.basename(tokens[0])
    if launcher == "uv" and len(tokens) >= 2 and tokens[1] == "run":
        return ["uv", "run", "pyfltr"]
    if launcher == "uvx":
        return ["uvx", "pyfltr"]
    return fallback


def build_retry_args_template(sys_args: list[str]) -> list[str]:
    result: list[str] = []
    i = 0
    while i < len(sys_args):
        arg = sys_args[i]
        if arg == "--commands":
            result.extend([arg, ""])
            i += 2
            continue
        if arg.startswith("--commands="):
            result.append("--commands=")
            i += 1
            continue
        if arg == "--only-failed":
            i += 1
            continue
        if arg == "--from-run":
            i += 2
            continue
        if arg.startswith("--from-run="):
            i += 1
            continue
        result.append(arg)
        i += 1
    return result


def build_retry_command(
    args_template: list[str],
    launcher_prefix: list[str],
    *,
    tool: str,
    target_files: list[pathlib.Path],
    target_base_cwd: str,
) -> str:
    cwd_path = pathlib.Path(target_base_cwd)
    filtered: list[str] = []
    seen_subcommand = False
    i = 0
    while i < len(args_template):
        arg = args_template[i]
        if arg.startswith("-"):
            filtered.append(arg)
            if "=" not in arg and i + 1 < len(args_template):
                next_arg = args_template[i + 1]
                if not next_arg.startswith("-") and _option_takes_value(arg):
                    filtered.append(next_arg)
                    i += 2
                    continue
            i += 1
            continue
        if not seen_subcommand:
            filtered.append(arg)
            seen_subcommand = True
            i += 1
            continue
        i += 1

    replaced: list[str] = []
    j = 0
    commands_replaced = False
    while j < len(filtered):
        arg = filtered[j]
        if arg == "--commands":
            replaced.extend(["--commands", tool])
            commands_replaced = True
            j += 2 if j + 1 < len(filtered) else 1
            continue
        if arg == "--commands=":
            replaced.append(f"--commands={tool}")
            commands_replaced = True
            j += 1
            continue
        if arg.startswith("--commands="):
            replaced.append(f"--commands={tool}")
            commands_replaced = True
            j += 1
            continue
        replaced.append(arg)
        j += 1
    if not commands_replaced:
        insert_at = 0
        for k, tok in enumerate(replaced):
            if not tok.startswith("-"):
                insert_at = k + 1
                break
        replaced[insert_at:insert_at] = ["--commands", tool]

    target_strs: list[str] = []
    for target in target_files:
        if target.is_absolute():
            target_strs.append(str(target))
        else:
            target_strs.append(str((cwd_path / target).resolve(strict=False)))

    parts: list[str] = [*launcher_prefix, *replaced, *target_strs]
    return shlex.join(parts)


def populate_retry_command(
    result: pyfltr.command.core_.CommandResult,
    *,
    retry_args_template: list[str],
    launcher_prefix: list[str],
    target_base_cwd: str,
) -> None:
    if result.cached:
        return
    if not result.needs_rerun:
        return
    filtered_targets = filter_failed_files(result, start_cwd=pathlib.Path(target_base_cwd))
    result.retry_command = build_retry_command(
        retry_args_template,
        launcher_prefix,
        tool=result.command,
        target_files=filtered_targets,
        target_base_cwd=target_base_cwd,
    )


def filter_failed_files(result: pyfltr.command.core_.CommandResult, *, start_cwd: pathlib.Path) -> list[pathlib.Path]:
    if not result.errors:
        return []
    failed_keys = {pyfltr.paths.to_match_key(error.file, start_cwd=start_cwd) for error in result.errors if error.file}
    if not failed_keys:
        return []
    return [target for target in result.target_files if pyfltr.paths.to_match_key(target, start_cwd=start_cwd) in failed_keys]


def _option_takes_value(opt: str) -> bool:
    if opt.endswith("-args"):
        return True
    return opt in _VALUE_OPTIONS
