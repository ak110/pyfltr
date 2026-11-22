import pathlib
import typing

import pyfltr.config.model
import pyfltr.tools
import pyfltr.warnings_
from pyfltr.config.model import Config, resolve_subproject_aware


def build_fast_alias(config: Config) -> list[str]:
    return [
        name
        for name in config.command_names
        if pyfltr.config.model.command_setting(config.values, name, "fast", False)
        or (name == "pytest" and pytest_fast_target_globs(config.values))
    ]


def pytest_fast_target_globs(values: dict[str, typing.Any]) -> list[str]:
    return _glob_list(values, "pytest-fast-targets")


def pytest_always_target_globs(values: dict[str, typing.Any]) -> list[str]:
    return _glob_list(values, "pytest-always-targets")


def _glob_list(values: dict[str, typing.Any], key: str) -> list[str]:
    raw = values.get(key, [])
    if isinstance(raw, str):
        return [raw] if raw else []
    return [str(item) for item in raw]


def is_command_enabled_anywhere(
    command: str,
    config: Config,
    subproject_configs: dict[pathlib.Path, Config] | None = None,
) -> bool:
    if config.values.get(command) is True:
        return True
    if not subproject_configs:
        return False
    info = config.commands.get(command)
    if info is None:
        return False
    if not resolve_subproject_aware(config.values, command, info.subproject_aware):
        return False
    return any(sub_config.values.get(command) is True for sub_config in subproject_configs.values())


def filter_fix_commands(
    commands: list[str],
    config: Config,
    subproject_configs: dict[pathlib.Path, Config] | None = None,
) -> list[str]:
    result: list[str] = []
    for command in commands:
        if not is_command_enabled_anywhere(command, config, subproject_configs):
            continue
        if f"{command}-fix-args" in config.values:
            result.append(command)
    return result


def resolve_aliases(commands: list[str], config: Config) -> list[str]:
    result: list[str] = []
    for _ in range(10):
        result = []
        resolved: bool = False
        for command in commands:
            command = command.strip()
            if command in config["aliases"]:
                for c in config["aliases"][command]:
                    if c not in result:
                        result.append(c)
                resolved = True
            else:
                if command not in result:
                    result.append(command)
        if not resolved:
            break
        commands = result

    result.sort(key=lambda name: pyfltr.tools.command_index(config.command_names, name))
    return result
