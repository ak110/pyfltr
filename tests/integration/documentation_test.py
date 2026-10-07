"""利用ガイドに写した一覧と公開定義の整合。"""

import pathlib
import re

import pytest

import pyfltr.cli.mcp_server
import pyfltr.config.model

ROOT = pathlib.Path(__file__).resolve().parents[2]


def documented_aliases(source: str) -> dict[str, list[str]]:
    """単独実行の節からエイリアス一覧を読み取る。"""
    section = source.split("## 特定のツールのみ実行", 1)[1].split("## UI", 1)[0]
    aliases: dict[str, list[str]] = {}
    for name in ("format", "lint", "test", "audit"):
        match = re.search(rf"^- `{name}`:(.*(?:\n    .*)*)", section, re.MULTILINE)
        assert match is not None, name
        aliases[name] = re.findall(r"`([^`]+)`", match.group(1))
    return aliases


def test_documented_aliases_match_defaults() -> None:
    source = (ROOT / "docs/guide/usage.md").read_text(encoding="utf-8")
    expected = pyfltr.config.model.DEFAULT_CONFIG["aliases"]
    assert documented_aliases(source) == {name: expected[name] for name in ("format", "lint", "test", "audit")}


@pytest.mark.asyncio
async def test_documented_mcp_tools_match_server() -> None:
    source = (ROOT / "docs/guide/usage.md").read_text(encoding="utf-8")
    table = source.split("提供するMCPツール", 1)[1].split("コーディングエージェント側への", 1)[0]
    documented = re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE)
    tools = await pyfltr.cli.mcp_server.build_server().list_tools()
    assert len(documented) == len(set(documented))
    assert set(documented) == {tool.name for tool in tools}
