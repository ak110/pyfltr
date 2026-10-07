"""設定に基づくコマンドと対象の選択。"""

import pathlib
import typing

import pyfltr.config.model
import pyfltr.tools
import pyfltr.warnings_
from pyfltr.config.model import Config, resolve_subproject_aware


def build_fast_alias(config: Config) -> list[str]:
    """per-command fastフラグからfastエイリアスを動的構築。

    pytestは`pytest-fast-targets`が非空なら`pytest-fast`の値に依らず含める。
    """
    return [
        name
        for name in config.command_names
        if pyfltr.config.model.command_setting(config.values, name, "fast", False)
        or (name == "pytest" and pytest_fast_target_globs(config.values))
    ]


def pytest_fast_target_globs(values: dict[str, typing.Any]) -> list[str]:
    """`pytest-fast-targets`の値をglobのリストとして返す（未指定・空なら空リスト）。"""
    raw = values.get("pytest-fast-targets", [])
    if isinstance(raw, str):
        return [raw] if raw else []
    return [str(item) for item in raw]


def is_command_enabled_anywhere(
    command: str,
    config: Config,
    subproject_configs: dict[pathlib.Path, Config] | None = None,
) -> bool:
    """コマンドを実行対象として有効化するかを、起点と各サブプロジェクトの和集合で判定する。

    起点 config で有効なら常に `True`。モノレポで対象のコマンドが `subproject_aware=True` の場合に限り、
    いずれかのサブプロジェクト config で有効なら `True` を返す（親OFF・子ON対応）。
    `subproject_aware` 判定はツール特性を表すメタ設定のため起点 config で固定し、
    `subproject_aware=False` のリポジトリ単位ツールは起点設定のみで判定する。
    `subproject_configs` が空または `None`（単一プロジェクト）のときは起点設定のみで判定する。
    """
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
    """fixステージで実行すべきコマンドに限定する。

    `pyfltr run` / `pyfltr fast`のfixステージはlinterのautofix機能
    （`{command}-fix-args`）を前段で呼び出すための段で、formatterは対象外。
    formatter本体は通常ステージで常に書き込みモードで動くため、fixステージで
    重複して実行する必要はない。

    enabledかつ`{command}-fix-args`が定義されているlinter/testerを返す。
    モノレポでは起点と各サブプロジェクトの和集合で有効判定する（親OFF・子ON対応）。
    """
    result: list[str] = []
    for command in commands:
        if not is_command_enabled_anywhere(command, config, subproject_configs):
            continue
        if f"{command}-fix-args" in config.values:
            result.append(command)
    return result


def resolve_aliases(commands: list[str], config: Config) -> list[str]:
    """エイリアスを展開する。

    展開後のコマンド列は`config.command_names`の登録順でソートする。
    未知コマンドは末尾扱いとし、`command_names.index`の`ValueError`を発生させない。
    """
    # 最大10回まで再帰的に展開
    result: list[str] = []
    for _ in range(10):
        result = []
        resolved: bool = False
        for command in commands:
            command = command.strip()
            if command in config["aliases"]:
                for c in config["aliases"][command]:
                    if c not in result:  # 順番は維持しつつ重複排除
                        result.append(c)
                resolved = True
            else:
                if command not in result:  # 順番は維持しつつ重複排除
                    result.append(command)
        if not resolved:
            break
        commands = result

    # 未知コマンドは末尾扱いとし、`command_names.index`の`ValueError`を発生させない。
    # 検出は呼び出し側（pipeline.py側のparser.error整形）に委ねる方針。
    unknown_index = len(config.command_names)

    def _sort_key(name: str) -> int:
        try:
            return config.command_names.index(name)
        except ValueError:
            return unknown_index

    result.sort(key=_sort_key)
    return result
