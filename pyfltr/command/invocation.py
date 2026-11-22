import os

import pyfltr.command.structured_output
import pyfltr.config.model
from pyfltr.tools import AUTO_ARGS, AUTO_VALUE_ARGS


def _strip_format_option(args: list[str]) -> list[str]:
    result: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--format", "-f"):
            skip_next = True
            continue
        if arg.startswith("--format="):
            continue
        result.append(arg)
    return result


def _build_auto_args(command: str, config: pyfltr.config.model.Config, user_args: list[str]) -> list[str]:
    auto_entries = AUTO_ARGS.get(command, [])
    value_entries = AUTO_VALUE_ARGS.get(command, [])
    if not auto_entries and not value_entries:
        return []
    user_args_joined = " ".join(user_args)
    result: list[str] = []
    for flag_key, args in auto_entries:
        if not config.values.get(flag_key, False):
            continue
        for arg in args:
            if arg not in user_args_joined:
                result.append(arg)
    for value_key, template in value_entries:
        try:
            value = int(config.values.get(value_key, 0))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        flag = template.split("=", maxsplit=1)[0]
        if flag in user_args_joined:
            continue
        result.append(template.format(value=value))
    return result


def expanduser_args(values: list[str]) -> list[str]:
    return [_expanduser_arg(value) for value in values]


def _expanduser_arg(value: str) -> str:
    expanded = os.path.expanduser(value)
    if expanded != value:
        return expanded
    sep_index = value.find("=")
    if sep_index < 0:
        return value
    prefix = value[: sep_index + 1]
    rest = value[sep_index + 1 :]
    expanded_rest = os.path.expanduser(rest)
    if expanded_rest == rest:
        return value
    return prefix + expanded_rest


def resolve_user_args(command: str, config: pyfltr.config.model.Config) -> list[str]:
    args = expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "args", [])))
    extend_args = expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "extend-args", [])))
    return args + extend_args


def build_invocation_argv(
    command: str,
    config: pyfltr.config.model.Config,
    commandline_prefix: list[str],
    additional_args: list[str],
    *,
    fix_stage: bool,
) -> list[str]:
    user_args: list[str] = resolve_user_args(command, config)
    extra: list[str] = list(additional_args)

    if fix_stage and command == "textlint":
        fix_args: list[str] = expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "fix-args", [])))
        return [
            *commandline_prefix,
            *_strip_format_option(user_args),
            *fix_args,
            *extra,
            "--no-cache",
        ]

    fix_args_value: list[str] | None = None
    if fix_stage:
        raw = pyfltr.config.model.command_setting(config.values, command, "fix-args", None)
        fix_args_value = expanduser_args(list(raw)) if raw is not None else None

    auto_args = _build_auto_args(command, config, user_args + extra)
    commandline: list[str] = [*commandline_prefix, *auto_args, *user_args]
    if fix_args_value is not None:
        commandline.extend(fix_args_value)
    else:
        commandline.extend(expanduser_args(list(pyfltr.config.model.command_setting(config.values, command, "lint-args", []))))
    commandline.extend(extra)
    structured_spec = pyfltr.command.structured_output.get_structured_output_spec(command, config)
    if structured_spec is not None and not (structured_spec.lint_only and fix_args_value is not None):
        commandline = pyfltr.command.structured_output.apply_structured_output(commandline, structured_spec)
    return commandline
