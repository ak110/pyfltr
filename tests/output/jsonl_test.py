import json

import pyfltr.cli.main
import pyfltr.command.core_
import pyfltr.command.mise
import pyfltr.command.snapshot
import pyfltr.config.config
import pyfltr.diagnostics
import pyfltr.output.jsonl
import pyfltr.output.jsonl_records


def test_dump_roundtrip() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="`os` imported but unused",
        rule="F401",
        severity="error",
        fix="safe",
    )
    result = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff", "check"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        errors=[error],
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_command_lines(result, config)
    parsed = json.loads(lines[0])
    assert parsed["kind"] == "diagnostic"
    assert parsed["command"] == "ruff-check"
    assert parsed["messages"][0]["rule"] == "F401"


def test_build_command_lines_truncates_diagnostics_when_archived() -> None:
    errors = [
        pyfltr.diagnostics.ErrorLocation(file="src/foo.py", line=i, col=None, command="mypy", message=f"err{i}")
        for i in range(10)
    ]
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        errors=errors,
        archived=True,
    )
    config = pyfltr.config.config.create_default_config()
    config.values["jsonl-diagnostic-limit"] = 3
    lines = pyfltr.output.jsonl.build_command_lines(result, config)
    assert len(lines) == 2
    diag_record = json.loads(lines[0])
    assert diag_record["kind"] == "diagnostic"
    assert len(diag_record["messages"]) == 3
    tool_record = json.loads(lines[-1])
    assert tool_record["diagnostics"] == 3
    assert tool_record["truncated"]["diagnostics_total"] == 10
    assert tool_record["truncated"]["archive"] == "tools/mypy/diagnostics.jsonl"


def test_build_command_lines_no_truncation_when_not_archived() -> None:
    errors = [
        pyfltr.diagnostics.ErrorLocation(file="src/foo.py", line=i, col=None, command="mypy", message=f"err{i}")
        for i in range(10)
    ]
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        errors=errors,
        archived=False,
    )
    config = pyfltr.config.config.create_default_config()
    config.values["jsonl-diagnostic-limit"] = 3
    lines = pyfltr.output.jsonl.build_command_lines(result, config)
    assert len(lines) == 2
    diag_record = json.loads(lines[0])
    assert len(diag_record["messages"]) == 10
    tool_record = json.loads(lines[-1])
    assert tool_record["diagnostics"] == 10
    assert "truncated" not in tool_record


def test_command_result_status_warning_when_severity_warning() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="colloquial",
        command_type="linter",
        commandline=["uv", "run"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        severity="warning",
    )
    assert result.status == "warning"


def test_command_result_status_failed_when_severity_error() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        severity="error",
    )
    assert result.status == "failed"


def test_command_result_status_resolution_failed_ignores_severity() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="colloquial",
        command_type="linter",
        commandline=[],
        returncode=1,
        files=0,
        output="",
        elapsed=0.0,
        resolution_failed=True,
        severity="warning",
    )
    assert result.status == "resolution_failed"


def test_command_result_status_timeout_ignores_severity() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="colloquial",
        command_type="linter",
        commandline=["uv"],
        returncode=137,
        files=1,
        output="",
        elapsed=600.0,
        timeout_exceeded=True,
        severity="warning",
    )
    assert result.status == "failed"
