"""コマンド実行順の制御。"""

import pyfltr.config


def split_commands_for_execution(commands: list[str], config: pyfltr.config.Config) -> tuple[list[str], list[str]]:
    """有効なコマンドをフェーズごとに分割。

    linters/testersはfastでないもの（重いツール）を先に並べて、
    並列実行時に重いツールが先に開始されるようにする。
    """
    formatters: list[str] = []
    linters_and_testers: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        if config.commands[command].type == "formatter":
            formatters.append(command)
        else:
            linters_and_testers.append(command)
    # fastでないもの（重いツール）を先に実行開始
    linters_and_testers.sort(key=lambda c: config.values.get(f"{c}-fast", False))
    return formatters, linters_and_testers
