"""設定ファイル欠落警告（`config-files`）が今回の実行対象だけを判定することの結合テスト。

CLIの`pyfltr.cli.main.run`とMCPの`tool_run`は共通の実行パイプラインを経由する。
判定が設定上の有効化だけへ戻ると未選択コマンドの警告が返り、判定位置が実行経路から外れると
選択したコマンドの警告が消えるため、両方の失敗様態をCLIとMCPの双方で検出する。
"""

from __future__ import annotations

import pathlib
import subprocess
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_server
import pyfltr.warnings_

# 設定上は有効だが`--commands`で選択しないprek・textlintと、選択するpytestを持つ設定。
# 起点直下に`.pre-commit-config.yaml`・`.textlintrc*`を置かず、選択すれば警告される状態にする。
LIMITED_CONFIG = "pytest = true\nprek = true\ntextlint = true\n"

# `config-files`を持つカスタムコマンド。`fast-tool`だけを`fast`エイリアスへ含める。
CUSTOM_CONFIG = """
[tool.pyfltr.custom-commands.fast-tool]
type = "linter"
path = "fast-tool"
targets = ["*.txt"]
fast = true
config-files = [".fasttoolrc*"]

[tool.pyfltr.custom-commands.slow-tool]
type = "linter"
path = "slow-tool"
targets = ["*.txt"]
config-files = [".slowtoolrc"]
"""


def _write_project(path: pathlib.Path, *, tool_pyfltr: str, extra: str = "") -> None:
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "proj"\n[tool.pyfltr]\nrespect-gitignore = false\n{tool_pyfltr}{extra}',
        encoding="utf-8",
    )
    (path / "tests").mkdir()
    (path / "tests" / "sample_test.py").write_text("def test_sample(): pass\n", encoding="utf-8")
    (path / "input.txt").write_text("hello\n", encoding="utf-8")


def _succeed(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(commandline, returncode=0, stdout="")


def _missing_config_commands(messages: typing.Iterable[str]) -> list[str]:
    """設定欠落警告の本文から対象コマンド名を取り出して返す。"""
    marker = " が有効化されていますが、設定ファイルが見つかりません"
    return sorted(message.split(marker, 1)[0] for message in messages if marker in message)


def _cli_missing(tmp_path: pathlib.Path, *argv: str) -> list[str]:
    pyfltr.cli.main.run([*argv, "--work-dir", str(tmp_path), "--no-archive", "--no-cache"])
    return _missing_config_commands(w["message"] for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config")


async def _mcp_missing(tmp_path: pathlib.Path, **kwargs: typing.Any) -> list[str]:
    result = await pyfltr.cli.mcp_server.tool_run(paths=["."], work_dir=str(tmp_path), no_cache=True, **kwargs)
    return _missing_config_commands(w.message for w in result.warnings if w.source == "config")


def test_cli_limited_run_omits_unselected_command_warnings(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """設定上有効でも`--commands`で選ばなかったprek・textlintの設定欠落警告を返さない。"""
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG)
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "fast", "--commands=pytest")

    assert not missing
    assert any("pytest" in " ".join(call.args[0]) for call in mock_run.call_args_list)


@pytest.mark.asyncio
async def test_mcp_limited_run_omits_unselected_command_warnings(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """MCPの`commands=["pytest"]`でも未選択コマンドの設定欠落警告を返さない。"""
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = await _mcp_missing(tmp_path, mode="fast", commands=["pytest"])

    assert not missing


def test_cli_selected_commands_missing_config_are_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """選択した組込み・カスタムコマンドの設定が欠けていれば、それぞれ1件ずつ警告する。"""
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=prek,slow-tool")

    assert missing == ["prek", "slow-tool"]


@pytest.mark.asyncio
async def test_mcp_selected_commands_missing_config_are_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """MCPでも選択したコマンドの設定欠落警告を返す。"""
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = await _mcp_missing(tmp_path, commands=["prek", "slow-tool"])

    assert missing == ["prek", "slow-tool"]


def test_cli_selected_command_with_config_present_is_not_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """選択したコマンドの候補（globを含む）のいずれかが存在すれば警告しない。"""
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (tmp_path / ".fasttoolrc.json").write_text("{}\n", encoding="utf-8")
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=prek,fast-tool")

    assert not missing


def test_cli_disabled_command_is_not_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """選択しても`--disable`で無効化したコマンドは実行対象外のため警告しない。"""
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=prek,slow-tool", "--disable=prek")

    assert missing == ["slow-tool"]


def test_cli_alias_expansion_selects_warned_commands(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """`fast`エイリアスの展開結果に含まれるカスタムコマンドだけを判定する。"""
    _write_project(tmp_path, tool_pyfltr="", extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=fast")

    assert missing == ["fast-tool"]


@pytest.mark.asyncio
async def test_mcp_fast_mode_selects_warned_commands(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    """MCPの`mode="fast"`でも`fast`対象外のカスタムコマンドを判定しない。"""
    _write_project(tmp_path, tool_pyfltr="", extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = await _mcp_missing(tmp_path, mode="fast")

    assert missing == ["fast-tool"]
