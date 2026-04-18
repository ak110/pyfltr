"""llm_outputのテストコード。"""
# pylint: disable=protected-access,duplicate-code

import json

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.llm_output


def test_build_diagnostic_record_with_rule_severity_fix() -> None:
    """rule・severity・fixフィールドがdiagnosticレコードに含まれることのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="`os` imported but unused",
        rule="F401",
        severity="error",
        fix="safe",
    )
    record = pyfltr.llm_output._build_diagnostic_record(error)
    assert record["kind"] == "diagnostic"
    assert record["tool"] == "ruff-check"
    assert record["file"] == "src/foo.py"
    assert record["line"] == 10
    assert record["col"] == 5
    assert record["rule"] == "F401"
    assert record["severity"] == "error"
    assert record["fix"] == "safe"
    assert record["msg"] == "`os` imported but unused"

    # msgは最後のキーであることを確認（フィールド順序）
    keys = list(record.keys())
    assert keys[-1] == "msg"


def test_build_diagnostic_record_none_fields_omitted() -> None:
    """rule・severity・fixがNoneのときフィールドが省略されることのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=None,
        command="mypy",
        message="Name 'x' is not defined",
    )
    record = pyfltr.llm_output._build_diagnostic_record(error)
    assert "col" not in record
    assert "rule" not in record
    assert "severity" not in record
    assert "fix" not in record
    assert record["msg"] == "Name 'x' is not defined"


def test_build_diagnostic_record_partial_fields() -> None:
    """一部のフィールドのみ設定されている場合のテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="pylint",
        message="Missing docstring",
        rule="C0114",
        severity="warning",
    )
    record = pyfltr.llm_output._build_diagnostic_record(error)
    assert record["rule"] == "C0114"
    assert record["severity"] == "warning"
    assert "fix" not in record


def test_dump_roundtrip() -> None:
    """_dump()のJSON出力がパース可能であることのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="`os` imported but unused",
        rule="F401",
        severity="error",
        fix="safe",
    )
    record = pyfltr.llm_output._build_diagnostic_record(error)
    line = pyfltr.llm_output._dump(record)
    parsed = json.loads(line)
    assert parsed["rule"] == "F401"
    assert parsed["severity"] == "error"
    assert parsed["fix"] == "safe"


def test_build_warning_record() -> None:
    """warning dict が kind/source/msg を持つレコードに変換される。"""
    record = pyfltr.llm_output._build_warning_record({"source": "config", "message": "foo"})
    assert record == {"kind": "warning", "source": "config", "msg": "foo"}


def test_build_warning_record_with_hint() -> None:
    """hint があれば warning レコードに hint キーが含まれる。"""
    record = pyfltr.llm_output._build_warning_record(
        {"source": "textlint-identifier-corruption", "message": "foo", "hint": "fooをバックティックで囲む"}
    )
    assert record == {
        "kind": "warning",
        "source": "textlint-identifier-corruption",
        "msg": "foo",
        "hint": "fooをバックティックで囲む",
    }


def test_build_diagnostic_record_rule_url_included() -> None:
    """rule_url が設定されていれば diagnostic レコードに含まれる。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="x",
        rule="F401",
        rule_url="https://docs.astral.sh/ruff/rules/F401/",
    )
    record = pyfltr.llm_output._build_diagnostic_record(error)
    assert record["rule_url"] == "https://docs.astral.sh/ruff/rules/F401/"
    # 順序: rule → rule_url → severity → fix → msg
    keys = list(record.keys())
    assert keys.index("rule") < keys.index("rule_url") < keys.index("msg")


def test_build_diagnostic_record_rule_url_omitted_when_none() -> None:
    """rule_url が None の場合はフィールドが省略される。"""
    error = pyfltr.error_parser.ErrorLocation(file="src/foo.py", line=10, col=None, command="mypy", message="x")
    record = pyfltr.llm_output._build_diagnostic_record(error)
    assert "rule_url" not in record


def test_build_tool_record_retry_command_included() -> None:
    """retry_command が設定されていれば tool レコードに含まれる (失敗時のみ populate される前提)。"""
    result = pyfltr.command.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff", "check"],
        returncode=1,
        has_error=True,
        files=3,
        output="",
        elapsed=0.5,
        retry_command="pyfltr run --commands ruff-check -- src/foo.py",
    )
    record = pyfltr.llm_output._build_tool_record(result, diagnostics=0)
    assert record["retry_command"] == "pyfltr run --commands ruff-check -- src/foo.py"


def test_build_tool_record_retry_command_omitted() -> None:
    """retry_command が None の場合、tool レコードから省略される。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.llm_output._build_tool_record(result, diagnostics=0)
    assert "retry_command" not in record


def test_build_tool_lines_truncates_diagnostics_when_archived() -> None:
    """jsonl-diagnostic-limit 超過時、先頭 N 件に切り詰めて truncated メタを付与する。"""
    errors = [
        pyfltr.error_parser.ErrorLocation(file="src/foo.py", line=i, col=None, command="mypy", message=f"err{i}")
        for i in range(10)
    ]
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
        errors=errors,
        archived=True,
    )
    config = pyfltr.config.create_default_config()
    config.values["jsonl-diagnostic-limit"] = 3
    lines = pyfltr.llm_output.build_tool_lines(result, config)
    # diagnostic 3 行 + tool 行 = 4 行
    assert len(lines) == 4
    tool_record = json.loads(lines[-1])
    assert tool_record["diagnostics"] == 3
    assert tool_record["truncated"]["diagnostics_total"] == 10
    assert tool_record["truncated"]["archive"] == "tools/mypy/diagnostics.jsonl"


def test_build_tool_lines_no_truncation_when_not_archived() -> None:
    """archived=False のときは切り詰めをスキップして全件出力する。"""
    errors = [
        pyfltr.error_parser.ErrorLocation(file="src/foo.py", line=i, col=None, command="mypy", message=f"err{i}")
        for i in range(10)
    ]
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
        errors=errors,
        archived=False,
    )
    config = pyfltr.config.create_default_config()
    config.values["jsonl-diagnostic-limit"] = 3
    lines = pyfltr.llm_output.build_tool_lines(result, config)
    # 切り詰めなし: diagnostic 10 行 + tool 行
    assert len(lines) == 11
    tool_record = json.loads(lines[-1])
    assert tool_record["diagnostics"] == 10
    assert "truncated" not in tool_record


def test_build_tool_record_cached_includes_cached_from() -> None:
    """cached=True のとき cached/cached_from が tool レコードに含まれる。"""
    result = pyfltr.command.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        has_error=False,
        files=3,
        output="",
        elapsed=0.0,
        cached=True,
        cached_from="01ABCDEFGH",
    )
    record = pyfltr.llm_output._build_tool_record(result, diagnostics=0)
    assert record["cached"] is True
    assert record["cached_from"] == "01ABCDEFGH"


def test_build_tool_record_cached_omitted_when_false() -> None:
    """cached=False の場合は cached/cached_from が省略される。"""
    result = pyfltr.command.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        has_error=False,
        files=3,
        output="",
        elapsed=0.0,
    )
    record = pyfltr.llm_output._build_tool_record(result, diagnostics=0)
    assert "cached" not in record
    assert "cached_from" not in record


def test_build_tool_record_message_truncated_when_archived() -> None:
    """failed + message 切り詰め時、truncated に lines / chars / archive が入る。"""
    many_lines = "\n".join(f"line{i}" for i in range(100))
    result = pyfltr.command.CommandResult(
        command="shellcheck",
        command_type="linter",
        commandline=["shellcheck"],
        returncode=1,
        has_error=True,
        files=1,
        output=many_lines,
        elapsed=0.1,
        archived=True,
    )
    config = pyfltr.config.create_default_config()
    record = pyfltr.llm_output._build_tool_record(result, diagnostics=0, config=config)
    assert "message" in record
    assert record["message"].startswith("... (truncated)")
    assert record["truncated"]["archive"] == "tools/shellcheck/output.log"
    assert record["truncated"]["lines"] == 100


def test_build_header_record_contains_schema_hints() -> None:
    """header レコードに schema_hints が含まれ、代表的な英語キーが埋まっている。"""
    record = pyfltr.llm_output._build_header_record(commands=["ruff-check"], files=3, run_id="01TESTULID")
    assert record["run_id"] == "01TESTULID"
    hints = record.get("schema_hints")
    assert isinstance(hints, dict)
    assert "diagnostic.fix" in hints
    assert "tool.retry_command" in hints
    assert "header.run_id" in hints
    # 値は英語で LLM が読む前提
    assert "auto-fix" in hints["diagnostic.fix"]


def test_build_summary_record_emits_guidance_on_failure() -> None:
    """failed > 0 のとき summary.guidance が英語で付与され、launcher_prefix と run_id が埋め込まれる。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.llm_output._build_summary_record(
        [result],
        exit_code=1,
        run_id="01JABCDEFGH",
        launcher_prefix=["uvx", "pyfltr"],
    )
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    assert guidance
    joined = " ".join(guidance)
    assert "retry_command" in joined
    assert "uvx pyfltr run-for-agent --only-failed" in joined
    assert "uvx pyfltr show-run 01JABCDEFGH" in joined
    # プレースホルダが残っていないこと
    assert "<run_id>" not in joined


def test_build_summary_record_guidance_falls_back_when_unspecified() -> None:
    """run_id / launcher_prefix 未指定時はプレースホルダ・既定値にフォールバックする。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.llm_output._build_summary_record([result], exit_code=1)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    joined = " ".join(guidance)
    assert "pyfltr show-run <run_id>" in joined
    assert "pyfltr run-for-agent --only-failed" in joined


def test_build_summary_record_no_guidance_on_success() -> None:
    """failed == 0 のときは summary.guidance が省略される。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.llm_output._build_summary_record([result], exit_code=0)
    assert "guidance" not in record
