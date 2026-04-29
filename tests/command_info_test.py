"""`command-info` サブコマンドの単体テスト。"""

from __future__ import annotations

import argparse
import json
import subprocess

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
    """cargo-fmtの既定設定（mise.toml記述なし）でmise形式のコマンドラインがセクション付きで表示される。

    autouseフィクスチャ `_default_mise_active_tools_empty` により判定辞書は空（記述なし）扱い。
    したがって従来通り `<backend>@latest` を組み立てる経路が選ばれる。
    """
    out = _run("cargo-fmt", capsys=capsys)
    assert "# cargo-fmt" in out
    # セクション見出しが付与されること。
    assert "## 実行コマンド" in out
    assert "## ランナー解決" in out
    assert "## 設定" in out
    assert "runner: bin-runner (default)" in out
    assert "effective_runner: mise" in out
    assert "commandline: mise exec rust@latest -- cargo" in out
    # cargo-fmtはfix-args未定義のため、fix stepは併記されない。
    assert "commandline (fix step)" not in out
    assert "commandline (check step)" not in out


def test_command_info_text_cargo_fmt_with_mise_active(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    """mise.tomlに `rust` 記述があるとtool specを省略した `mise exec -- cargo` 形になる。"""
    monkeypatch.setattr(
        "pyfltr.command._get_mise_active_tools",
        lambda config, *, allow_side_effects=False: {"rust": [{"version": "1.83.0"}]},
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "commandline: mise exec -- cargo" in out
    # tool spec省略でも `runner: bin-runner` / `effective_runner: mise` は維持される。
    assert "effective_runner: mise" in out


def test_command_info_check_passes_allow_side_effects_true(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """`--check` 真時は `_get_mise_active_tools` へ `allow_side_effects=True` が渡る。"""
    spy = mocker.patch("pyfltr.command._get_mise_active_tools", return_value={})
    # ensure_mise_available 内のsubprocess.runは成功扱いに固定する（FileNotFoundErrorで失敗しないため）。
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""))
    _run("cargo-fmt", do_check=True, capsys=capsys)
    # 少なくとも1回は allow_side_effects=True で呼ばれている。
    assert any(call.kwargs.get("allow_side_effects") is True for call in spy.call_args_list)


def test_command_info_no_check_passes_allow_side_effects_false(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """`--check` 偽時は `_get_mise_active_tools` へ `allow_side_effects=False` が渡る。"""
    spy = mocker.patch("pyfltr.command._get_mise_active_tools", return_value={})
    _run("cargo-fmt", capsys=capsys)
    # 副作用なし契約のため `allow_side_effects=True` での呼び出しが発生しないこと。
    assert all(call.kwargs.get("allow_side_effects") is False for call in spy.call_args_list)
    assert spy.call_count >= 1


def test_command_info_text_textlint_includes_fix_step(capsys: pytest.CaptureFixture[str]) -> None:
    """fix-args定義済みコマンド（textlint）ではfix step / check stepが併記される。"""
    out = _run("textlint", capsys=capsys)
    assert "commandline (fix step):" in out
    assert "commandline (check step):" in out
    fix_line = next(line for line in out.splitlines() if line.startswith("commandline (fix step):"))
    check_line = next(line for line in out.splitlines() if line.startswith("commandline (check step):"))
    assert "--fix" in fix_line
    # check stepには構造化出力（textlint-json既定有効）の`--format json`が注入される。
    assert "--format json" in check_line
    # fix stepでは`--format`ペアが除去され、`--fix`のみが結合される（textlint特殊経路）。
    assert "--format" not in fix_line


def test_command_info_text_markdownlint_includes_fix_step(capsys: pytest.CaptureFixture[str]) -> None:
    """markdownlintもfix-args既定値があるため両ステップが併記される。"""
    out = _run("markdownlint", capsys=capsys)
    assert "commandline (fix step):" in out
    assert "commandline (check step):" in out


def test_command_info_json_typos(capsys: pytest.CaptureFixture[str]) -> None:
    """typosのjson出力に必要なキーが揃っている。"""
    out = _run("typos", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["command"] == "typos"
    assert info["runner"] == "direct"
    assert info["effective_runner"] == "direct"
    assert info["commandline"][0].endswith("typos") or info["commandline"][0] == "typos"
    # typosはfix-args未定義のためfix_commandlineキーは含まれない。
    assert "fix_commandline" not in info


def test_command_info_json_textlint_has_fix_commandline(capsys: pytest.CaptureFixture[str]) -> None:
    """fix-args定義済みコマンドのjson出力にはfix_commandlineキーが含まれる。"""
    out = _run("textlint", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert "commandline" in info
    assert "fix_commandline" in info
    # fix stepは--fixを含み、check stepは構造化出力経由で--format jsonを含む。
    assert "--fix" in info["fix_commandline"]
    assert "json" in info["commandline"]


def test_command_info_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    """未知のコマンド名はエラー終了する。"""
    args = argparse.Namespace(command="not-a-tool", output_format="text", check=False)
    rc = pyfltr.command_info.execute_command_info(args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "未知のコマンド" in captured.err


def test_command_info_does_not_invoke_mise(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """既定（--check未指定）ではmise exec --versionなどのsubprocessを発火しない。"""
    spy = mocker.patch("subprocess.run")
    _run("cargo-fmt", capsys=capsys)
    assert spy.call_count == 0
