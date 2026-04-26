"""``command-info`` サブコマンドの単体テスト。"""

from __future__ import annotations

import argparse
import json

import pytest

import pyfltr.command_info
import pyfltr.config


def _run(command: str, *, output_format: str = "text", do_check: bool = False, capsys: pytest.CaptureFixture[str]) -> str:
    args = argparse.Namespace(command=command, output_format=output_format, check=do_check)
    rc = pyfltr.command_info.execute_command_info(args)
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return captured.out


def test_command_info_text_cargo_fmt(capsys: pytest.CaptureFixture[str]) -> None:
    """cargo-fmt の既定設定で mise 形式のコマンドラインが表示される。"""
    out = _run("cargo-fmt", capsys=capsys)
    assert "command: cargo-fmt" in out
    assert "runner: bin-runner (default)" in out
    assert "effective_runner: mise" in out
    assert "commandline: mise exec rust@latest -- cargo" in out


def test_command_info_json_typos(capsys: pytest.CaptureFixture[str]) -> None:
    """typos の json 出力に必要なキーが揃っている。"""
    out = _run("typos", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["command"] == "typos"
    assert info["runner"] == "direct"
    assert info["effective_runner"] == "direct"
    assert info["commandline"][0].endswith("typos") or info["commandline"][0] == "typos"


def test_command_info_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    """未知のコマンド名はエラー終了する。"""
    args = argparse.Namespace(command="not-a-tool", output_format="text", check=False)
    rc = pyfltr.command_info.execute_command_info(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "未知のコマンド" in captured.err


def test_command_info_does_not_invoke_mise(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """既定（--check 未指定）では mise exec --version などの subprocess を発火しない。"""
    spy = mocker.patch("subprocess.run")
    _run("cargo-fmt", capsys=capsys)
    assert spy.call_count == 0
