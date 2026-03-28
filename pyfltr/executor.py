"""コマンド実行順の制御。"""

import pyfltr.config


def split_commands_for_execution(commands: list[str], config: pyfltr.config.Config) -> tuple[list[str], list[str]]:
    """有効なコマンドをフェーズごとに分割。"""
    formatters: list[str] = []
    linters_and_testers: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        if config.commands[command].type == "formatter":
            formatters.append(command)
        else:
            linters_and_testers.append(command)
    return formatters, linters_and_testers
