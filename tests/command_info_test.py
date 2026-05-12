"""`command-info` サブコマンドの単体テスト。"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess

import pytest

import pyfltr.cli.command_info
import pyfltr.command.mise
import pyfltr.command.runner
import pyfltr.config.config


def _run(
    command: str, *, output_format: str | None = "text", do_check: bool = False, capsys: pytest.CaptureFixture[str]
) -> str:
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(command=command, output_format=output_format, check=do_check)
    rc = pyfltr.cli.command_info.execute_command_info(parser, args)
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
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "commandline: mise exec -- cargo" in out
    # tool spec省略でも `runner: bin-runner` / `effective_runner: mise` は維持される。
    assert "effective_runner: mise" in out


def test_command_info_check_passes_allow_side_effects_true(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """`--check` 真時は `get_mise_active_tools` へ `allow_side_effects=True` が渡る。"""
    spy = mocker.patch(
        "pyfltr.command.mise.get_mise_active_tools",
        return_value=pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )
    # ensure_mise_available 内のsubprocess.runは成功扱いに固定する（FileNotFoundErrorで失敗しないため）。
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""))
    _run("cargo-fmt", do_check=True, capsys=capsys)
    # 少なくとも1回は allow_side_effects=True で呼ばれている。
    assert any(call.kwargs.get("allow_side_effects") is True for call in spy.call_args_list)


def test_command_info_no_check_passes_allow_side_effects_false(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """`--check` 偽時は `get_mise_active_tools` へ `allow_side_effects=False` が渡る。"""
    spy = mocker.patch(
        "pyfltr.command.mise.get_mise_active_tools",
        return_value=pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )
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
    assert pathlib.Path(info["commandline"][0]).stem == "typos"
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


def test_command_info_ai_agent_default_json(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """`AI_AGENT`設定時の`command-info`既定出力はjson形式になる。"""
    monkeypatch.setenv("AI_AGENT", "1")
    out = _run("typos", output_format=None, capsys=capsys)
    info = json.loads(out)
    assert info["command"] == "typos"
    assert info["runner"] == "direct"


def test_command_info_pyfltr_env_overrides_ai_agent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`PYFLTR_OUTPUT_FORMAT=text`は`AI_AGENT`より優先され、textが選ばれる。"""
    monkeypatch.setenv("AI_AGENT", "1")
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "text")
    out = _run("typos", output_format=None, capsys=capsys)
    assert out.startswith("# typos")


def test_command_info_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    """未知のコマンド名はエラー終了する。"""
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(command="not-a-tool", output_format="text", check=False)
    rc = pyfltr.cli.command_info.execute_command_info(parser, args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "未知のコマンド" in captured.err


@pytest.mark.parametrize(
    "command,expect_suggestion",
    [
        # typoしきい値内 → サジェスト候補が並ぶ
        ("pylit", True),
        # 完全に無関係 → 候補無し
        ("totally-unrelated", False),
    ],
)
def test_command_info_unknown_command_suggestion(
    capsys: pytest.CaptureFixture[str], command: str, expect_suggestion: bool
) -> None:
    """未知のコマンドにdifflibのサジェストが付くか/付かないかを境界別に検証する。"""
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(command=command, output_format="text", check=False)
    pyfltr.cli.command_info.execute_command_info(parser, args)
    captured = capsys.readouterr()
    if expect_suggestion:
        assert "もしかして:" in captured.err
    else:
        assert "もしかして:" not in captured.err


def test_command_info_does_not_invoke_mise(capsys: pytest.CaptureFixture[str], mocker) -> None:
    """既定（--check未指定）ではmise exec --versionなどのsubprocessを発火しない。"""
    spy = mocker.patch("subprocess.run")
    _run("cargo-fmt", capsys=capsys)
    assert spy.call_count == 0


# --- mise診断フィールド（G+H+I+K） ---


def test_command_info_text_exposes_mise_tool_spec_omitted_false(capsys: pytest.CaptureFixture[str]) -> None:
    """既定（mise設定記述なし）ではtool spec省略未採用が `false` で露出する。"""
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_tool_spec_omitted: False" in out


def test_command_info_text_exposes_mise_tool_spec_omitted_true(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """mise設定にtool記述あり時は `true` で露出する。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_tool_spec_omitted: True" in out


def test_command_info_text_exposes_mise_active_tool_key(capsys: pytest.CaptureFixture[str]) -> None:
    """mise active toolsを引く際の照合キーが露出する。cargo-fmtは `rust`。"""
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tool_key: rust" in out


def test_command_info_text_aqua_active_tool_key(capsys: pytest.CaptureFixture[str]) -> None:
    """cargo-denyはaqua表記の照合キーが露出する（名称ずれ自己診断のため）。"""
    out = _run("cargo-deny", capsys=capsys)
    assert "mise_active_tool_key: aqua:EmbarkStudios/cargo-deny" in out


def test_command_info_text_omits_active_tool_key_for_python_tools(capsys: pytest.CaptureFixture[str]) -> None:
    """mise backend未登録ツール（ruff-check等）には判定キーフィールドを出力しない。"""
    out = _run("ruff-check", capsys=capsys)
    assert "mise_active_tool_key" not in out


def test_command_info_text_exposes_mise_active_tools_status_ok(capsys: pytest.CaptureFixture[str]) -> None:
    """既定（autouse fixtureでstatus=ok）でステータスが露出する。

    取得成功でキーが空の場合は`active_keys`行ごと省略され、他の任意フィールドの省略慣習と揃う。
    """
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tools.status: ok" in out
    assert "mise_active_tools.active_keys" not in out


def test_command_info_text_exposes_mise_active_tools_active_keys(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """mise active toolsが取得できた場合はキー一覧が露出する。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [], "python": []}
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tools.active_keys: python, rust" in out


def test_command_info_text_trust_hint_on_untrusted_no_check(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--check`無しかつ `untrusted-no-side-effects` ステータスでは `--check` 案内を1行出力する。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="untrusted-no-side-effects", detail="config not trusted"
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tools.status: untrusted-no-side-effects" in out
    assert "mise_active_tools.detail: config not trusted" in out
    assert "`--check`を付けるとtrust試行を行う" in out


def test_command_info_text_trust_hint_only_for_untrusted(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """他のエラー要因では trust案内を出力しない（ノイズを増やさないため）。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="exec-error", detail="something failed"
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tools.status: exec-error" in out
    assert "`--check`" not in out


def test_command_info_text_no_mise_section_for_python_tools(capsys: pytest.CaptureFixture[str]) -> None:
    """mise経路を使わないツールには mise診断セクションを出力しない。"""
    out = _run("ruff-check", capsys=capsys)
    assert "## mise診断" not in out
    assert "mise_active_tools" not in out


def test_command_info_json_includes_mise_fields(capsys: pytest.CaptureFixture[str]) -> None:
    """JSON出力にも新フィールドが含まれる（mise_tool_spec_omitted・mise_active_tool_key・mise_active_tools）。"""
    out = _run("cargo-fmt", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["mise_tool_spec_omitted"] is False
    assert info["mise_active_tool_key"] == "rust"
    assert info["mise_active_tools"]["status"] == "ok"
    assert not info["mise_active_tools"]["active_keys"]


def test_command_info_python_tool_shows_uv_info(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """`mypy` を対象に command-info を呼び出すと `uv_info` キーが含まれる。

    既定では `mypy-runner = "python-runner"` ＋ グローバル `python-runner = "uv"` のため
    `uv_info.mode == "uv"` となる。`runner` キー値はper-tool値で `"python-runner"`、
    `effective_runner` は委譲解決後の `"uv"` となる。
    """
    monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda: True)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: True)
    out = _run("mypy", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["runner"] == "python-runner"
    assert info["effective_runner"] == "uv"
    assert "uv_info" in info
    uv_info = info["uv_info"]
    assert uv_info["mode"] == "uv"
    assert uv_info["uv_available"] is True
    assert uv_info["uv_lock_present"] is True
    assert uv_info["direct_fallback"] is False
    assert uv_info["python_tool_bin"] == "mypy"


def test_command_info_python_tool_uv_info_shows_fallback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv.lock不在時は direct_fallback=True となる。"""
    monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda: False)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.shutil.which", lambda name: f"/fake/bin/{name}" if name == "mypy" else None)
    out = _run("mypy", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert "uv_info" in info
    uv_info = info["uv_info"]
    assert uv_info["uv_lock_present"] is False
    assert uv_info["direct_fallback"] is True


def test_command_info_python_tool_uv_info_path_override_keeps_fallback_false(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mypy-path` 指定 × uv/uv.lock 不在でも `direct_fallback` は False のまま。

    `direct_fallback` は「uv経路からdirectへフォールバックした」ことを示すフィールドのため、
    path-override経路で direct に解決された場合は uv 経路を辿っていない＝Falseが正しい。
    """
    monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda: False)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: False)
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-path"] = "/usr/bin/mypy-custom"
    monkeypatch.setattr(pyfltr.config.config, "load_config", lambda **_kw: config)
    out = _run("mypy", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert "uv_info" in info
    uv_info = info["uv_info"]
    assert uv_info["uv_available"] is False
    assert uv_info["uv_lock_present"] is False
    assert uv_info["direct_fallback"] is False


def test_command_info_non_uv_tool_has_no_uv_info(capsys: pytest.CaptureFixture[str]) -> None:
    """uv経路対象外ツール（cargo-fmt等）には uv_info が付かない。"""
    out = _run("cargo-fmt", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert "uv_info" not in info


def test_command_info_python_tool_uvx_runner_shows_uv_info(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mypy-runner = "uvx"` 設定時は `uv_info.mode == "uvx"` となる。

    uvx経路では`uv.lock`を参照しないため、`mode == "uv"`との判定差を確認する。
    `direct_fallback`はuvx shimの可用性のみで判定される。
    """
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uvx_available", lambda: True)
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-runner"] = "uvx"
    monkeypatch.setattr(pyfltr.config.config, "load_config", lambda **_kw: config)
    out = _run("mypy", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["runner"] == "uvx"
    assert info["effective_runner"] == "uvx"
    assert "uv_info" in info
    uv_info = info["uv_info"]
    assert uv_info["mode"] == "uvx"
    assert uv_info["uvx_available"] is True
    assert uv_info["direct_fallback"] is False


def test_command_info_python_tool_uvx_runner_fallback_when_uvx_missing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvx shim不在時は `direct_fallback=True` となる（mode="uvx"側の判定経路）。"""
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uvx_available", lambda: False)
    monkeypatch.setattr(
        "pyfltr.command.runner.shutil.which",
        lambda name: f"/fake/bin/{name}" if name == "mypy" else None,
    )
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-runner"] = "uvx"
    monkeypatch.setattr(pyfltr.config.config, "load_config", lambda **_kw: config)
    out = _run("mypy", output_format="json", capsys=capsys)
    info = json.loads(out)
    uv_info = info["uv_info"]
    assert uv_info["mode"] == "uvx"
    assert uv_info["uvx_available"] is False
    assert uv_info["direct_fallback"] is True


def test_command_info_python_tool_python_runner_direct_omits_uv_info(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`python-runner = "direct"` の場合はuv経路を辿らないため `uv_info` を出力しない。"""
    monkeypatch.setattr(
        "pyfltr.command.runner.shutil.which",
        lambda name: f"/fake/bin/{name}" if name == "mypy" else None,
    )
    config = pyfltr.config.config.create_default_config()
    config.values["python-runner"] = "direct"
    monkeypatch.setattr(pyfltr.config.config, "load_config", lambda **_kw: config)
    out = _run("mypy", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["runner"] == "python-runner"
    assert info["effective_runner"] == "direct"
    assert "uv_info" not in info


def test_command_info_text_python_tool_shows_uv_section(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """text形式でもuv診断セクションが表示される。"""
    monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda: True)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: True)
    out = _run("mypy", capsys=capsys)
    assert "## uv診断" in out
    assert "mode: uv" in out
    assert "uv_available: True" in out
    assert "uv_lock_present: True" in out
    assert "direct_fallback: False" in out
    assert "python_tool_bin: mypy" in out
