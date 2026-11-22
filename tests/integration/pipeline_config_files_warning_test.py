from __future__ import annotations

import pathlib
import subprocess
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_run_tools
import pyfltr.warnings_

LIMITED_CONFIG = "pytest = true\nprek = true\ntextlint = true\n"

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
    marker = " が有効化されていますが、設定ファイルが見つかりません"
    return sorted(message.split(marker, 1)[0] for message in messages if marker in message)


def _cli_missing(tmp_path: pathlib.Path, *argv: str) -> list[str]:
    pyfltr.cli.main.run([*argv, "--work-dir", str(tmp_path), "--no-archive", "--no-cache"])
    return _missing_config_commands(w["message"] for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config")


async def _mcp_missing(tmp_path: pathlib.Path, **kwargs: typing.Any) -> list[str]:
    result = await pyfltr.cli.mcp_run_tools.tool_run(paths=["."], work_dir=str(tmp_path), no_cache=True, **kwargs)
    return _missing_config_commands(w.message for w in result.warnings if w.source == "config")


def test_cli_limited_run_omits_unselected_command_warnings(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG)
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "fast", "--commands=pytest")

    assert not missing
    assert any("pytest" in " ".join(call.args[0]) for call in mock_run.call_args_list)


@pytest.mark.asyncio
async def test_mcp_limited_run_omits_unselected_command_warnings(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = await _mcp_missing(tmp_path, mode="fast", commands=["pytest"])

    assert not missing


def test_cli_selected_commands_missing_config_are_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=prek,slow-tool")

    assert missing == ["prek", "slow-tool"]


@pytest.mark.asyncio
async def test_mcp_selected_commands_missing_config_are_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = await _mcp_missing(tmp_path, commands=["prek", "slow-tool"])

    assert missing == ["prek", "slow-tool"]


def test_cli_selected_command_with_config_present_is_not_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    (tmp_path / ".fasttoolrc.json").write_text("{}\n", encoding="utf-8")
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=prek,fast-tool")

    assert not missing


def test_cli_disabled_command_is_not_warned(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr=LIMITED_CONFIG, extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=prek,slow-tool", "--disable=prek")

    assert missing == ["slow-tool"]


def test_cli_alias_expansion_selects_warned_commands(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr="", extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = _cli_missing(tmp_path, "run", "--commands=fast")

    assert missing == ["fast-tool"]


@pytest.mark.asyncio
async def test_mcp_fast_mode_selects_warned_commands(tmp_path: pathlib.Path, mocker: typing.Any) -> None:
    _write_project(tmp_path, tool_pyfltr="", extra=CUSTOM_CONFIG)
    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_succeed)

    missing = await _mcp_missing(tmp_path, mode="fast")

    assert missing == ["fast-tool"]
