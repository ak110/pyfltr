import argparse
import difflib

import pyfltr.cli.output_format
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection
import pyfltr.run_options


def apply_subcommand_defaults(args: argparse.Namespace | pyfltr.run_options.RunOptions) -> None:
    subcommand = args.subcommand
    args.include_fix_stage = subcommand in ("run", "fast", "run-for-agent")
    if subcommand in ("run", "fast", "run-for-agent"):
        args.exit_zero_even_if_formatted = True
    if subcommand == "fast" and args.commands is None:
        args.commands = ["fast"]
    if getattr(args, "quiet", None) is None:
        args.quiet = subcommand == "run-for-agent" or pyfltr.cli.output_format.detect_agent_indicator() is not None


def flatten_commands_arg(values: list[str] | None, config: pyfltr.config.model.Config) -> list[str]:
    if values is None:
        return list(config.command_names)
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        for name in raw.split(","):
            if name == "" or name in seen:
                continue
            seen.add(name)
            result.append(name)
    return result


CLI_LIST_COMMANDS = "`pyfltr config list --all`"
"""CLI利用者へ示す、登録済みコマンド名の確認手段。"""

MCP_LIST_COMMANDS = '`config`ツールの`action="list", include_defaults=True`'
"""MCP利用者へ示す、登録済みコマンド名の確認手段。"""


def format_unknown_command_message(
    command: str, config: pyfltr.config.model.Config, *, list_commands: str = CLI_LIST_COMMANDS
) -> str:
    suggestions = difflib.get_close_matches(command, list(config.commands), n=3, cutoff=0.6)
    parts = [f"コマンドが見つかりません: {command}"]
    if suggestions:
        parts.append(f"もしかして: {', '.join(suggestions)}")
    parts.append(
        f"ビルトインのコマンド名は {list_commands} で、カスタムコマンドは"
        " pyproject.tomlの `[tool.pyfltr.custom-commands]` で確認できます"
    )
    return "。".join(parts)


def validate_commands(
    commands: list[str], config: pyfltr.config.model.Config, *, list_commands: str = CLI_LIST_COMMANDS
) -> None:
    for command in commands:
        if command not in config.commands:
            raise ValueError(format_unknown_command_message(command, config, list_commands=list_commands))


def compute_unmet_commands(
    command_tokens: list[str],
    requested_commands: list[str],
    enabled_commands: list[str],
    config: pyfltr.config.model.Config,
) -> list[str]:
    enabled = set(enabled_commands)
    suppressed: set[str] = set()
    explicit: set[str] = set()
    for token in command_tokens:
        if token in config["aliases"]:
            expanded = pyfltr.config.selection.resolve_aliases([token], config)
            if any(name in enabled for name in expanded):
                suppressed.update(expanded)
        else:
            explicit.add(token)
    return [c for c in requested_commands if c not in enabled and (c in explicit or c not in suppressed)]
