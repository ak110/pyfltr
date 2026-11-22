import os
import pathlib
import subprocess
import time
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_
import tests.execution_request_helper
from tests import conftest as _testconf


def test_expanduser_expands_ruff_format_check_args(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format-check-args"] = ["check", "--fix", "--config=~/ruff.toml"]

    captured: list[list[str]] = []

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured.append(list(commandline))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)

    target = tmp_path / "x.py"
    target.write_text("")
    command_info = config.commands["ruff-format"]
    args_ns = pyfltr.run_options.RunOptions.from_values({"verbose": False})
    pyfltr.command.two_step.ruff.execute_ruff_format_two_step(
        tests.execution_request_helper.make_request(
            command="ruff-format",
            command_info=command_info,
            format_commandline=["ruff", "format", str(target)],
            commandline_prefix=["ruff"],
            targets=[target],
            config=config,
            args=args_ns,
            env={},
            on_output=None,
            start_time=time.perf_counter(),
        )
    )

    assert captured, "expected ruff-format two-step steps to run"
    assert "--config=/tmp/fake-home/ruff.toml" in captured[0]


def test_ruff_format_two_step_runs_check_and_format(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 2
    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]
    assert "check" in step1_cmdline
    assert "--fix" in step1_cmdline
    assert "--unsafe-fixes" in step1_cmdline
    assert "format" in step2_cmdline
    assert "--exit-non-zero-on-format" in step2_cmdline
    assert result.status == "succeeded"


def test_ruff_format_by_check_false_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    config.values["ruff-format-by-check"] = False
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "format" in cmdline
    assert "check" not in cmdline
    assert result.status == "succeeded"


def test_ruff_format_step1_lint_violation_ignored(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="lint violation")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "succeeded"
    assert result.formatter_failed is False


def test_ruff_format_step1_internal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=2, stdout="usage error")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.formatter_failed is True


def test_ruff_format_step2_internal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="format error")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.formatter_failed is True


def test_ruff_format_step1_mtime_change_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "check" in cmdline:
            target.write_text("x = 2\n")
            os.utime(target, (2000000000, 2000000000))
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "formatted"
    assert result.formatter_failed is False
