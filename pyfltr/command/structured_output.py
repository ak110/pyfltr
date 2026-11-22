import pathlib

import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.tool_types import StructuredOutputSpec


def get_structured_output_spec(command: str, config: pyfltr.config.model.Config) -> StructuredOutputSpec | None:
    info = pyfltr.tools.BUILTIN_COMMANDS.get(command)
    if info is None or info.structured_output is None:
        return None
    config_key, spec = info.structured_output
    return spec if config.values.get(config_key, False) else None


def apply_structured_output(commandline: list[str], spec: StructuredOutputSpec) -> list[str]:
    start = 0
    if commandline and pathlib.Path(commandline[0]).stem == "mise" and "--" in commandline:
        start = commandline.index("--") + 1
    marker = spec.before_argument
    separator = commandline.index(marker, start) if marker is not None and marker in commandline[start:] else len(commandline)
    tail = commandline[separator:]
    commandline = commandline[:separator]
    filtered: list[str] = []
    skip_next = False
    for i, arg in enumerate(commandline):
        if skip_next:
            skip_next = False
            continue
        matched = False
        for prefix in spec.conflicts:
            if arg == prefix:
                if i + 1 < len(commandline) and not commandline[i + 1].startswith("-"):
                    skip_next = True
                matched = True
                break
            if arg.startswith(f"{prefix}=") or (arg.startswith(prefix) and arg != prefix):
                matched = True
                break
        if not matched:
            filtered.append(arg)
    return [*filtered, *spec.inject, *tail]
