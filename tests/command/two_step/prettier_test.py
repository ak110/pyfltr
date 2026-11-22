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


def test_two_step_prettier_includes_extend_args(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    config = pyfltr.config.config.create_default_config()
    config.values["prettier-args"] = ["--base"]
    config.values["prettier-extend-args"] = ["--extra"]

    captured: list[list[str]] = []

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured.append(list(commandline))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=1, stdout="", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)

    target = tmp_path / "x.md"
    target.write_text("")
    command_info = config.commands["prettier"]
    args_ns = pyfltr.run_options.RunOptions.from_values({"verbose": False})
    pyfltr.command.two_step.prettier.execute_prettier_two_step(
        tests.execution_request_helper.make_request(
            command="prettier",
            command_info=command_info,
            commandline_prefix=["prettier"],
            config=config,
            targets=[target],
            additional_args=[],
            fix_mode=False,
            env={},
            on_output=None,
            start_time=time.perf_counter(),
            args=args_ns,
        )
    )

    assert len(captured) == 2, f"expected both check and write steps, got {captured}"
    for step in captured:
        assert "--extra" in step
        assert step.index("--extra") > step.index("--base")


def test_prettier_two_step_check_clean(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    proc = subprocess.CompletedProcess(["prettier"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--check" in cmdline
    assert "--write" not in cmdline
    assert result.status == "succeeded"
    assert result.formatter_failed is False


def test_prettier_two_step_check_needs_write(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="[warn] sample.js")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="sample.js")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "formatted"
    assert result.formatter_failed is False


def test_prettier_two_step_check_rc2_fails_without_write(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    calls: list[list[str]] = []

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        calls.append(cmdline)
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="SyntaxError")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.formatter_failed is True
    assert len(calls) == 1
    assert "--check" in calls[0]


def test_prettier_two_step_step2_failure_marks_failed(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="write failed")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.formatter_failed is True


def test_prettier_fix_mode_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        target.write_text("x = 1;\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--write" in cmdline
    assert "--check" not in cmdline
    assert result.status == "formatted"


def test_prettier_fix_mode_no_change_succeeds(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    proc = subprocess.CompletedProcess(["prettier"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "succeeded"
