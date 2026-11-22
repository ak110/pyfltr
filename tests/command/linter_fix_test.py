import os
import pathlib
import subprocess

import pytest

import pyfltr.command.dispatcher
import pyfltr.command.markdownlint_env
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection
from tests import conftest as _testconf


@pytest.fixture(autouse=True)
def _isolate_markdownlint_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setattr(
        pyfltr.command.markdownlint_env,
        "resolve_environment",
        lambda cwd: pyfltr.command.markdownlint_env.MarkdownlintEnvironment(tmp_path / "markdownlint-env", {}, cwd),
    )
    monkeypatch.setattr(pyfltr.command.markdownlint_env, "ensure_environment", lambda *_args, **_kwargs: None)


def test_fix_mode_appends_fix_args_for_linter(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["markdownlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "markdownlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert pathlib.Path(cmdline[0]).stem == "markdownlint-cli2"
    assert "--fix" in cmdline
    assert cmdline.index("--fix") > 0
    assert result.status == "succeeded"


def test_fix_mode_preserves_custom_args(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["markdownlint-cli2"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["markdownlint"] = True
    config.values["markdownlint-args"] = ["--config", "custom.yaml"]
    pyfltr.command.dispatcher.execute_command(
        "markdownlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert "--config" in cmdline
    assert "custom.yaml" in cmdline
    assert "--fix" in cmdline
    assert cmdline.index("custom.yaml") < cmdline.index("--fix")


def test_fix_mode_mtime_change_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        target.write_text("# Title\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["markdownlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "markdownlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    assert result.status == "formatted"
    assert result.formatter_failed is False


def test_fix_mode_non_zero_rc_is_failed(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        target.write_text("# Title\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout="violation remains")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-check", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "failed"
    assert result.failed is True


def test_eslint_lint_mode_uses_json_format(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("var x = 1;\n")

    proc = subprocess.CompletedProcess(["eslint"], returncode=0, stdout="[]")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["eslint"] = True
    pyfltr.command.dispatcher.execute_command(
        "eslint", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "json" in cmdline
    fmt_idx = cmdline.index("--format")
    assert cmdline[fmt_idx + 1] == "json"
    assert "--fix" not in cmdline


def test_eslint_fix_mode_appends_fix_and_keeps_json(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("var x = 1;\n")

    proc = subprocess.CompletedProcess(["eslint"], returncode=0, stdout="[]")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["eslint"] = True
    pyfltr.command.dispatcher.execute_command(
        "eslint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "json" in cmdline
    assert "--fix" in cmdline


def test_biome_lint_mode_uses_check_and_github_reporter(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["biome"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["biome"] = True
    pyfltr.command.dispatcher.execute_command(
        "biome", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "--reporter=github" in cmdline
    assert "--write" not in cmdline


def test_biome_fix_mode_appends_write_and_unsafe_and_keeps_reporter(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["biome"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["biome"] = True
    pyfltr.command.dispatcher.execute_command(
        "biome", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "--reporter=github" in cmdline
    assert "--write" in cmdline
    assert "--unsafe" in cmdline
    assert cmdline.index("check") < cmdline.index("--write")
    assert cmdline.index("check") < cmdline.index("--unsafe")


def _force_direct_runner(config: pyfltr.config.model.Config, command: str, mocker) -> None:
    config.values[f"{command}-runner"] = "direct"
    mocker.patch(
        "pyfltr.command.runner._resolve_direct_executable",
        side_effect=lambda bin_name: f"/usr/bin/{bin_name}",
    )


def test_cargo_fmt_runs_without_file_args(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt"] = True
    _force_direct_runner(config, "cargo-fmt", mocker)
    pyfltr.command.dispatcher.execute_command(
        "cargo-fmt", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert pathlib.Path(cmdline[0]).name == "cargo"
    assert cmdline[1:] == ["fmt"]
    assert str(target) not in cmdline


def test_cargo_fmt_fix_mode_unchanged(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt"] = True
    _force_direct_runner(config, "cargo-fmt", mocker)
    pyfltr.command.dispatcher.execute_command(
        "cargo-fmt", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert pathlib.Path(cmdline[0]).name == "cargo"
    assert cmdline[1:] == ["fmt"]


def test_cargo_clippy_normal_mode_cmdline(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["cargo-clippy"] = True
    _force_direct_runner(config, "cargo-clippy", mocker)
    pyfltr.command.dispatcher.execute_command(
        "cargo-clippy", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert pathlib.Path(cmdline[0]).name == "cargo"
    assert cmdline[1:] == _testconf.CARGO_CLIPPY_LINT_CMDLINE[1:]
    assert str(target) not in cmdline


def test_cargo_clippy_fix_mode_cmdline(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.rs"
    target.write_text("fn main() {}\n")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["cargo-clippy"] = True
    _force_direct_runner(config, "cargo-clippy", mocker)
    pyfltr.command.dispatcher.execute_command(
        "cargo-clippy", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert pathlib.Path(cmdline[0]).name == "cargo"
    assert cmdline[1:] == _testconf.CARGO_CLIPPY_FIX_CMDLINE[1:]
    assert str(target) not in cmdline


def test_dotnet_format_runs_without_file_args(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "Sample.cs"
    target.write_text("class Sample {}\n")

    proc = subprocess.CompletedProcess(["dotnet"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["dotnet-format"] = True
    _force_direct_runner(config, "dotnet-format", mocker)
    pyfltr.command.dispatcher.execute_command(
        "dotnet-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert pathlib.Path(cmdline[0]).name == "dotnet"
    assert cmdline[1:] == ["format"]
    assert str(target) not in cmdline


def test_cargo_test_skipped_when_no_rs_files(mocker) -> None:
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess")

    config = pyfltr.config.config.create_default_config()
    config.values["cargo-test"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "cargo-test", _testconf.make_args(), _testconf.make_execution_context(config, [])
    )

    assert mock_run.call_count == 0
    assert result.returncode is None
    assert result.files == 0


def test_tool_exclude_filters_files(mocker, tmp_path: pathlib.Path) -> None:
    kept = tmp_path / "main.py"
    excluded_ = tmp_path / "gen_foo.py"
    kept.write_text("x = 1\n")
    excluded_.write_text("x = 2\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-check-exclude"] = ["gen_*.py"]

    result = pyfltr.command.dispatcher.execute_command(
        "ruff-check", _testconf.make_args(), _testconf.make_execution_context(config, [kept, excluded_])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(kept) in cmdline
    assert str(excluded_) not in cmdline
    assert result.status == "succeeded"


def test_tool_exclude_disabled_by_no_exclude(mocker, tmp_path: pathlib.Path) -> None:
    kept = tmp_path / "main.py"
    would_be_excluded = tmp_path / "gen_foo.py"
    kept.write_text("x = 1\n")
    would_be_excluded.write_text("x = 2\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-check-exclude"] = ["gen_*.py"]

    result = pyfltr.command.dispatcher.execute_command(
        "ruff-check", _testconf.make_args(no_exclude=True), _testconf.make_execution_context(config, [kept, would_be_excluded])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(kept) in cmdline
    assert str(would_be_excluded) in cmdline
    assert result.status == "succeeded"
