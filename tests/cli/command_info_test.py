from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import typing

import pytest

import pyfltr.cli.command_info
import pyfltr.cli.output_format
import pyfltr.command.mise
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.tool_version
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
    out = _run("cargo-fmt", capsys=capsys)
    assert "# cargo-fmt" in out
    assert "## 実行コマンド" in out
    assert "## ランナー解決" in out
    assert "## 設定" in out
    assert "runner: bin-runner (default)" in out
    assert "effective_runner: mise" in out
    assert "commandline: mise exec rust@latest -- cargo" in out
    assert "commandline (fix step)" not in out
    assert "commandline (check step)" not in out


def test_command_info_text_cargo_fmt_with_mise_active(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "commandline: mise exec -- cargo" in out
    assert "effective_runner: mise" in out


def test_command_info_check_passes_allow_side_effects_true(capsys: pytest.CaptureFixture[str], mocker) -> None:
    spy = mocker.patch(
        "pyfltr.command.mise.get_mise_active_tools",
        return_value=pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""))
    version_probe = mocker.patch(
        "pyfltr.command.process.run_process_loop",
        side_effect=_fake_version_run("cargo 1.90.0"),
    )
    _run("cargo-fmt", do_check=True, capsys=capsys)
    assert any(call.kwargs.get("allow_side_effects") is True for call in spy.call_args_list)
    assert version_probe.call_count == 1


def test_command_info_no_check_passes_allow_side_effects_false(capsys: pytest.CaptureFixture[str], mocker) -> None:
    spy = mocker.patch(
        "pyfltr.command.mise.get_mise_active_tools",
        return_value=pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )
    _run("cargo-fmt", capsys=capsys)
    assert all(call.kwargs.get("allow_side_effects") is False for call in spy.call_args_list)
    assert spy.call_count >= 1


def _fake_version_run(
    version_output: str,
) -> typing.Callable[..., pyfltr.command.process.CompletedProcessWithTimeoutInfo]:

    def _run_version(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout=version_output, timeout_exceeded=False
        )

    return _run_version


def test_command_info_check_rejects_below_minimum_package_manager_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_version_run("uv 0.10.9"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    out = _run("uv-audit", do_check=True, capsys=capsys)

    assert "check_passed: False" in out
    assert "0.11.2以降" in out


def test_command_info_check_passes_with_sufficient_package_manager_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_version_run("uv 0.11.7"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    out = _run("uv-audit", do_check=True, capsys=capsys)

    assert "check_passed: True" in out


def test_command_info_no_check_skips_package_manager_version_probe(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    calls: list[list[str]] = []

    def _record(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        calls.append(list(commandline))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="uv 0.11.7", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _record)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    _run("uv-audit", capsys=capsys)

    assert not calls


def test_command_info_check_commandline_matches_final_argv(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_version_run("uv 0.11.7"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    info = json.loads(_run("uv-audit", output_format="json", do_check=True, capsys=capsys))

    assert info["check_passed"] is True
    assert info["check_commandline"] == info["commandline"]
    assert "--frozen" in info["check_commandline"]


def test_command_info_check_commandline_keeps_configured_args_on_fallback(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda name, path=None: None if pathlib.Path(name).stem == "mise" else f"/usr/bin/{name}",
    )

    info = json.loads(_run("cargo-fmt", output_format="json", do_check=True, capsys=capsys))

    assert info["check_passed"] is True
    assert info["check_effective_runner"] == "direct"
    assert info["check_commandline"] == ["/usr/bin/cargo", "fmt"]
    assert info["check_commandline"] != info["commandline"]


def test_command_info_check_includes_installed_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_version_run("uv 0.11.7"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    info = json.loads(_run("uv-audit", output_format="json", do_check=True, capsys=capsys))

    assert info["check_installed_version"] == "uv 0.11.7"


def test_command_info_check_installed_version_none_when_unavailable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    probe_outputs = iter(("uv 0.11.7", "ready"))

    def _run_version(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout=next(probe_outputs), timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _run_version)
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    info = json.loads(_run("uv-audit", output_format="json", do_check=True, capsys=capsys))

    assert info["check_installed_version"] is None


def test_command_info_without_check_omits_installed_version(capsys: pytest.CaptureFixture[str]) -> None:
    info = json.loads(_run("uv-audit", output_format="json", capsys=capsys))

    assert "check_installed_version" not in info


def test_command_info_check_failure_omits_installed_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_version_run("uv 0.10.9"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    info = json.loads(_run("uv-audit", output_format="json", do_check=True, capsys=capsys))

    assert info["check_passed"] is False
    assert "check_installed_version" not in info


def test_command_info_text_shows_installed_version(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_version_run("uv 0.11.7"))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    out = _run("uv-audit", do_check=True, capsys=capsys)

    assert "check_installed_version: uv 0.11.7" in out


def test_command_info_text_textlint_includes_fix_step(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("textlint", capsys=capsys)
    assert "commandline (fix step):" in out
    assert "commandline (check step):" in out
    fix_line = next(line for line in out.splitlines() if line.startswith("commandline (fix step):"))
    check_line = next(line for line in out.splitlines() if line.startswith("commandline (check step):"))
    assert "--fix" in fix_line
    assert "--format json" in check_line
    assert "--format" not in fix_line


def test_command_info_text_markdownlint_includes_fix_step(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("markdownlint", capsys=capsys)
    assert "commandline (fix step):" in out
    assert "commandline (check step):" in out


def test_command_info_json_typos(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("typos", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["command"] == "typos"
    assert info["runner"] == "direct"
    assert info["effective_runner"] == "direct"
    assert pathlib.Path(info["commandline"][0]).stem == "typos"
    assert "fix_commandline" not in info


def test_command_info_json_textlint_has_fix_commandline(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("textlint", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert "commandline" in info
    assert "fix_commandline" in info
    assert "--fix" in info["fix_commandline"]
    assert "json" in info["commandline"]


@pytest.mark.parametrize("env_name", pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)
def test_command_info_agent_indicator_default_jsonl(
    env_name: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_name, "1")
    out = _run("typos", output_format=None, capsys=capsys)
    assert out.endswith("\n")
    assert "\n" not in out[:-1]
    info = json.loads(out)
    assert info["command"] == "typos"
    assert info["runner"] == "direct"


def test_command_info_jsonl_typos(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("typos", output_format="jsonl", capsys=capsys)
    assert out.endswith("\n")
    assert "\n" not in out[:-1]
    info = json.loads(out)
    assert info["command"] == "typos"
    assert info["runner"] == "direct"
    assert info["effective_runner"] == "direct"


@pytest.mark.parametrize("env_name", pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)
def test_command_info_pyfltr_env_overrides_agent_indicator(
    env_name: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(env_name, "1")
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "text")
    out = _run("typos", output_format=None, capsys=capsys)
    assert out.startswith("# typos")


def test_command_info_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(command="not-a-tool", output_format="text", check=False)
    rc = pyfltr.cli.command_info.execute_command_info(parser, args)
    captured = capsys.readouterr()
    assert rc == 1
    assert "コマンドが見つかりません: not-a-tool" in captured.err
    assert "pyfltr config list --all" in captured.err


@pytest.mark.parametrize(
    "command,expect_suggestion",
    [
        ("pylit", True),
        ("totally-unrelated", False),
    ],
)
def test_command_info_unknown_command_suggestion(
    capsys: pytest.CaptureFixture[str], command: str, expect_suggestion: bool
) -> None:
    parser = argparse.ArgumentParser()
    args = argparse.Namespace(command=command, output_format="text", check=False)
    pyfltr.cli.command_info.execute_command_info(parser, args)
    captured = capsys.readouterr()
    if expect_suggestion:
        assert "もしかして:" in captured.err
    else:
        assert "もしかして:" not in captured.err


def test_command_info_does_not_invoke_mise(capsys: pytest.CaptureFixture[str], mocker) -> None:
    spy = mocker.patch("subprocess.run")
    _run("cargo-fmt", capsys=capsys)
    assert spy.call_count == 0


def test_command_info_text_exposes_mise_tool_spec_omitted_false(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_tool_spec_omitted: False" in out


def test_command_info_text_exposes_mise_tool_spec_omitted_true(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_tool_spec_omitted: True" in out


def test_command_info_text_exposes_mise_active_tool_key(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tool_key: rust" in out


def test_command_info_text_aqua_active_tool_key(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("cargo-deny", capsys=capsys)
    assert "mise_active_tool_key: aqua:EmbarkStudios/cargo-deny" in out


def test_command_info_text_omits_active_tool_key_for_python_tools(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("ruff-check", capsys=capsys)
    assert "mise_active_tool_key" not in out


def test_command_info_text_exposes_mise_active_tools_status_ok(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tools.status: ok" in out
    assert "mise_active_tools.active_keys" not in out


def test_command_info_text_exposes_mise_active_tools_active_keys(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [], "python": []}
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tools.active_keys: python, rust" in out


def test_command_info_text_trust_hint_on_untrusted_no_check(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
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
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False, cwd=None: pyfltr.command.mise.MiseActiveToolsResult(
            status="exec-error", detail="something failed"
        ),
    )
    out = _run("cargo-fmt", capsys=capsys)
    assert "mise_active_tools.status: exec-error" in out
    assert "`--check`" not in out


def test_command_info_text_no_mise_section_for_python_tools(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("ruff-check", capsys=capsys)
    assert "## mise診断" not in out
    assert "mise_active_tools" not in out


def test_command_info_json_includes_mise_fields(capsys: pytest.CaptureFixture[str]) -> None:
    out = _run("cargo-fmt", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert info["mise_tool_spec_omitted"] is False
    assert info["mise_active_tool_key"] == "rust"
    assert info["mise_active_tools"]["status"] == "ok"
    assert not info["mise_active_tools"]["active_keys"]


def test_command_info_python_tool_shows_uv_info(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
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
    out = _run("cargo-fmt", output_format="json", capsys=capsys)
    info = json.loads(out)
    assert "uv_info" not in info


def test_command_info_python_tool_uvx_runner_shows_uv_info(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
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
    monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda: True)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: True)
    out = _run("mypy", capsys=capsys)
    assert "## uv診断" in out
    assert "mode: uv" in out
    assert "uv_available: True" in out
    assert "uv_lock_present: True" in out
    assert "direct_fallback: False" in out
    assert "python_tool_bin: mypy" in out
