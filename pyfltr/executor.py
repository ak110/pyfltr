"""コマンド実行順の制御。"""

import pyfltr.config


def split_commands_for_execution(
    commands: list[str],
    config: pyfltr.config.Config,
    *,
    fix_mode: bool = False,
) -> tuple[list[str], list[str]]:
    """有効なコマンドをフェーズごとに分割。

    linters/testersはfastでないもの（重いツール）を先に並べて、
    並列実行時に重いツールが先に開始されるようにする。

    fix_mode=True のときは、対象コマンドを全て順次実行バケツ (formatters) に積み、
    linters/testers バケツは空にする (同一ファイルへの書き込み競合を避けるため
    並列実行を停止する)。
    """
    formatters: list[str] = []
    linters_and_testers: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        if fix_mode:
            # fix モードでは全て順次実行バケツに積む
            formatters.append(command)
        elif config.commands[command].type == "formatter":
            formatters.append(command)
        else:
            linters_and_testers.append(command)
    # fastでないもの（重いツール）を先に実行開始
    linters_and_testers.sort(key=lambda c: config.values.get(f"{c}-fast", False))
    return formatters, linters_and_testers
