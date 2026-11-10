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
    """aggregate_diagnosticsで生成したdiagnosticレコードがJSON往復可能であることのテスト。"""
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
    # 最初の行がdiagnosticレコード
    parsed = json.loads(lines[0])
    assert parsed["kind"] == "diagnostic"
    assert parsed["command"] == "ruff-check"
    assert parsed["messages"][0]["rule"] == "F401"


def test_build_warning_record() -> None:
    """warning dictがkind/source/msg/hintを持つレコードに変換される。"""
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [],
        config,
        exit_code=0,
        warnings=[{"source": "config", "message": "foo", "hint": "設定値を修正してください。"}],
    )
    # summary行の前にwarning行が出力される
    warning_line = next(line for line in lines if json.loads(line).get("kind") == "warning")
    record = json.loads(warning_line)
    assert record == {"kind": "warning", "source": "config", "msg": "foo", "hint": "設定値を修正してください。"}


def test_build_warning_record_with_hint() -> None:
    """hintがあればwarningレコードにhintキーが含まれる。"""
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [],
        config,
        exit_code=0,
        warnings=[{"source": "textlint-identifier-corruption", "message": "foo", "hint": "fooをバックティックで囲む"}],
    )
    warning_line = next(line for line in lines if json.loads(line).get("kind") == "warning")
    record = json.loads(warning_line)
    assert record == {
        "kind": "warning",
        "source": "textlint-identifier-corruption",
        "msg": "foo",
        "hint": "fooをバックティックで囲む",
    }


def test_build_command_lines_truncates_diagnostics_when_archived() -> None:
    """jsonl-diagnostic-limit超過時、先頭N件の個別指摘に切り詰めてから集約する。"""
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
    # 3件とも同一fileのため集約後は1 diagnostic行 + tool行 = 2行
    assert len(lines) == 2
    diag_record = json.loads(lines[0])
    assert diag_record["kind"] == "diagnostic"
    assert len(diag_record["messages"]) == 3
    tool_record = json.loads(lines[-1])
    assert tool_record["diagnostics"] == 3
    assert tool_record["truncated"]["diagnostics_total"] == 10
    assert tool_record["truncated"]["archive"] == "tools/mypy/diagnostics.jsonl"


def test_build_command_lines_no_truncation_when_not_archived() -> None:
    """archived=Falseのときは切り詰めをスキップして全件出力する。"""
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
    # 切り詰めなし: 同一fileのため集約後は1 diagnostic行 + tool行 = 2行、messages 10件
    assert len(lines) == 2
    diag_record = json.loads(lines[0])
    assert len(diag_record["messages"]) == 10
    tool_record = json.loads(lines[-1])
    assert tool_record["diagnostics"] == 10
    assert "truncated" not in tool_record


def test_command_result_status_warning_when_severity_warning() -> None:
    """severity="warning"設定下では通常失敗が status="warning" に格下げされる。"""
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
    """severity="error"（既定）は従来通りfailedを返す。"""
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
    """resolution_failedはseverity="warning"でもresolution_failedのまま。"""
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
    """timeout_exceededはseverity="warning"でもfailedのまま。"""
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
