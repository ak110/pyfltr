"""llmstxtのサブコマンド・対応ツール網羅チェック。

mkdocs.yml の llmstxt プラグイン設定 markdown_description が
実装の全サブコマンド名と全ビルトインコマンド名を含んでいることを検査する。
"""

import argparse
import functools
import pathlib

import yaml

import pyfltr.cli.main
import pyfltr.cli.parser
import pyfltr.command.builtin


@functools.lru_cache(maxsize=1)
def _get_markdown_description() -> str:
    """mkdocs.yml の llmstxt.markdown_description を文字列として返す。

    mkdocs.yml は MkDocs 固有の `!!python/name:` タグを含むため SafeLoader では
    パースできない。カスタムタグを無視する Loader を用意して読み込む。
    """

    # !!python/name: などPython固有タグを文字列として無視するカスタムLoaderを作成する。
    # yaml.add_multi_constructor はグローバル状態を変更するため、Loader サブクラスに局所化する。
    class _IgnoreTagLoader(yaml.SafeLoader):  # pylint: disable=too-many-ancestors  # PyYAMLの継承木に従うため不可避
        pass

    def _ignore_python_tag(_loader: _IgnoreTagLoader, tag_suffix: str, _node: yaml.Node) -> str:
        # タグ付き値はダミー文字列として返す
        return f"<{tag_suffix}>"

    _IgnoreTagLoader.add_multi_constructor("tag:yaml.org,2002:python/name:", _ignore_python_tag)

    mkdocs_path = pathlib.Path(__file__).parent.parent / "mkdocs.yml"
    with mkdocs_path.open(encoding="utf-8") as f:
        data = yaml.load(f, Loader=_IgnoreTagLoader)  # noqa: S506  # 信頼済みリポジトリ内ファイルを読むため安全
    plugins = data.get("plugins", [])
    for plugin in plugins:
        if isinstance(plugin, dict) and "llmstxt" in plugin:
            return plugin["llmstxt"].get("markdown_description", "")
    return ""


def _get_subcommand_names() -> list[str]:
    """pyfltr.cli.parser.build_parser() から全サブコマンド名を取得する。"""
    parser = pyfltr.cli.parser.build_parser()
    subcommand_names: list[str] = []
    # argparse はサブパーサー一覧を公開 API で列挙する手段を提供しないため、
    # `_actions` / `_SubParsersAction` 経由の参照を使う。
    for action in parser._actions:  # type: ignore[attr-defined]  # pylint: disable=protected-access
        if isinstance(action, argparse._SubParsersAction):  # type: ignore[attr-defined]  # pylint: disable=protected-access
            subcommand_names.extend(action.choices.keys())
    return subcommand_names


def test_llmstxt_contains_all_subcommands() -> None:
    """markdown_description が全サブコマンド名を含むことを検査する。"""
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
    """markdown_description が全ビルトインコマンド名を含むことを検査する。"""
    description = _get_markdown_description()
    assert description, "mkdocs.yml から markdown_description を取得できなかった"

    command_names = pyfltr.command.builtin.BUILTIN_COMMAND_NAMES
    assert command_names, "ビルトインコマンド名を取得できなかった"

    missing = [name for name in command_names if name not in description]
    assert not missing, (
        f"markdown_description に以下のビルトインコマンドが見つからない: {missing}\n"
        f"mkdocs.yml の llmstxt.markdown_description の対応ツール記述を更新してください。"
    )
