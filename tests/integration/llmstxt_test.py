import argparse
import functools
import pathlib

import yaml

import pyfltr.cli.main
import pyfltr.cli.parser
import pyfltr.tools


@functools.lru_cache(maxsize=1)
def _get_markdown_description() -> str:

    class _IgnoreTagLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # PyYAMLの継承木に従うため不可避
        pass

    def _ignore_python_tag(_loader: _IgnoreTagLoader, tag_suffix: str, _node: yaml.Node) -> str:
        return f"<{tag_suffix}>"

    _IgnoreTagLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", _ignore_python_tag)

    mkdocs_path = pathlib.Path(__file__).parents[2] / "mkdocs.yml"
    with mkdocs_path.open(encoding="utf-8") as f:
        data = yaml.load(f, Loader=_IgnoreTagLoader)  # noqa: S506  # 信頼済みリポジトリ内ファイルを読むため安全
    plugins = data.get("plugins", [])
    for plugin in plugins:
        if isinstance(plugin, dict) and "llmstxt" in plugin:
            return plugin["llmstxt"].get("markdown_description", "")
    return ""


def _get_subcommand_names() -> list[str]:
    parser = pyfltr.cli.parser.build_parser()
    subcommand_names: list[str] = []
    for action in parser._actions:  # pylint: disable=protected-access  # type: ignore[attr-defined]
        if isinstance(action, argparse._SubParsersAction):  # pylint: disable=protected-access  # type: ignore[attr-defined]
            subcommand_names.extend(action.choices.keys())
    return subcommand_names


def test_llmstxt_contains_all_subcommands() -> None:
    description = _get_markdown_description()
    assert description, "mkdocs.yml から markdown_description を取得できなかった"

    subcommand_names = _get_subcommand_names()
    assert subcommand_names, "サブコマンド名を取得できなかった"

    missing = [name for name in subcommand_names if name not in description]
    assert not missing, (
        f"markdown_description に以下のサブコマンドが見つからない: {missing}\n"
        f"mkdocs.yml の llmstxt.markdown_description を更新してください。"
    )


def test_llmstxt_contains_all_builtin_commands() -> None:
    description = _get_markdown_description()
    assert description, "mkdocs.yml から markdown_description を取得できなかった"

    command_names = pyfltr.tools.BUILTIN_COMMAND_NAMES
    assert command_names, "ビルトインコマンド名を取得できなかった"

    missing = [name for name in command_names if name not in description]
    assert not missing, (
        f"markdown_description に以下のビルトインコマンドが見つからない: {missing}\n"
        f"mkdocs.yml の llmstxt.markdown_description の対応ツール記述を更新してください。"
    )
