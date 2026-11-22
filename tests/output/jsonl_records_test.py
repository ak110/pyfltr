import json
import pathlib
import sys

import pytest

import pyfltr.cli.main
import pyfltr.command.core_
import pyfltr.command.mise
import pyfltr.command.snapshot
import pyfltr.config.config
import pyfltr.diagnostics
import pyfltr.output.jsonl
import pyfltr.output.jsonl_records


def _parse_command_record(result: pyfltr.command.core_.CommandResult) -> dict:
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_command_lines(result, config)
    return json.loads(lines[-1])


def test_build_message_dict_with_rule_severity_fix() -> None:
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
    records, _, _ = pyfltr.output.jsonl_records.aggregate_diagnostics([error])
    messages = records[0]["messages"]
    assert len(messages) == 1
    message = messages[0]
    assert message["line"] == 10
    assert message["col"] == 5
    assert message["rule"] == "F401"
    assert message["severity"] == "error"
    assert message["fix"] == "safe"
    assert message["msg"] == "`os` imported but unused"

    keys = list(message.keys())
    assert keys[-1] == "msg"
    assert "rule_url" not in message


def test_build_message_dict_none_fields_omitted() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=None,
        command="mypy",
        message="Name 'x' is not defined",
    )
    records, _, _ = pyfltr.output.jsonl_records.aggregate_diagnostics([error])
    message = records[0]["messages"][0]
    assert "col" not in message
    assert "rule" not in message
    assert "severity" not in message
    assert "fix" not in message
    assert message["msg"] == "Name 'x' is not defined"


def test_build_message_dict_partial_fields() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="pylint",
        message="Missing docstring",
        rule="C0114",
        severity="warning",
    )
    records, _, _ = pyfltr.output.jsonl_records.aggregate_diagnostics([error])
    message = records[0]["messages"][0]
    assert message["rule"] == "C0114"
    assert message["severity"] == "warning"
    assert "fix" not in message


def test_aggregate_diagnostics_groups_by_tool_and_file() -> None:
    errors = [
        pyfltr.diagnostics.ErrorLocation(file="src/a.py", line=10, col=3, command="ruff-check", message="msg10b", rule="E501"),
        pyfltr.diagnostics.ErrorLocation(file="src/a.py", line=10, col=3, command="ruff-check", message="msg10a", rule="E401"),
        pyfltr.diagnostics.ErrorLocation(file="src/a.py", line=5, col=None, command="ruff-check", message="msg5"),
        pyfltr.diagnostics.ErrorLocation(file="src/b.py", line=1, col=None, command="ruff-check", message="msgB"),
    ]
    records, hint_urls, hints = pyfltr.output.jsonl_records.aggregate_diagnostics(errors)
    assert len(records) == 2
    assert records[0]["command"] == "ruff-check"
    assert records[0]["file"] == "src/a.py"
    assert [m.get("rule") for m in records[0]["messages"]] == [None, "E401", "E501"]
    assert [m["line"] for m in records[0]["messages"]] == [5, 10, 10]
    assert records[1]["file"] == "src/b.py"
    assert not hint_urls
    assert not hints


def test_aggregate_diagnostics_collects_hint_urls() -> None:
    errors = [
        pyfltr.diagnostics.ErrorLocation(
            file="a.py",
            line=1,
            col=None,
            command="ruff-check",
            message="m1",
            rule="F401",
            rule_url="https://docs.astral.sh/ruff/rules/F401/",
        ),
        pyfltr.diagnostics.ErrorLocation(
            file="b.py",
            line=2,
            col=None,
            command="ruff-check",
            message="m2",
            rule="F401",
            rule_url="https://docs.astral.sh/ruff/rules/F401/",
        ),
        pyfltr.diagnostics.ErrorLocation(
            file="a.py",
            line=3,
            col=None,
            command="ruff-check",
            message="m3",
            rule="E501",
        ),
    ]
    _, hint_urls, _ = pyfltr.output.jsonl_records.aggregate_diagnostics(errors)
    assert hint_urls == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_build_command_record_includes_hint_urls_when_provided() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py",
        line=1,
        col=None,
        command="ruff-check",
        message="unused import",
        rule="F401",
        rule_url="https://docs.astral.sh/ruff/rules/F401/",
    )
    result = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        errors=[error],
    )
    record = _parse_command_record(result)
    assert record["hint_urls"] == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_build_command_record_omits_hint_urls_when_empty() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = _parse_command_record(result)
    assert "hint_urls" not in record


def test_build_command_record_retry_command_included() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff", "check"],
        returncode=1,
        files=3,
        output="",
        elapsed=0.5,
        retry_command="pyfltr run --commands ruff-check -- src/foo.py",
    )
    record = _parse_command_record(result)
    assert record["retry_command"] == "pyfltr run --commands ruff-check -- src/foo.py"


def test_build_command_record_retry_command_omitted() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = _parse_command_record(result)
    assert "retry_command" not in record


def test_build_command_record_retry_count_included() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        retry_count=2,
    )
    record = _parse_command_record(result)
    assert record["retry_count"] == 2


def test_build_command_record_retry_count_omitted_when_zero() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = _parse_command_record(result)
    assert "retry_count" not in record


def test_build_command_record_omits_runner_info_when_normal_path() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["uv", "run", "--frozen", "mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        effective_runner="uv",
        runner_source="default",
    )
    record = _parse_command_record(result)
    assert "effective_runner" not in record
    assert "runner_source" not in record
    assert "runner_fallback" not in record


def test_build_command_record_runner_info_emitted_on_uv_fallback() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["/usr/bin/mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        effective_runner="direct",
        runner_source="default",
        runner_fallback="uv->direct",
    )
    record = _parse_command_record(result)
    assert record["effective_runner"] == "direct"
    assert record["runner_source"] == "default"
    assert record["runner_fallback"] == "uv->direct"
    keys = list(record.keys())
    assert keys.index("status") < keys.index("effective_runner") < keys.index("runner_source")
    assert keys.index("runner_source") < keys.index("runner_fallback") < keys.index("files")


def test_build_command_record_runner_info_emitted_on_mise_fallback() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="cargo-fmt",
        command_type="formatter",
        commandline=["/usr/bin/cargo"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        effective_runner="direct",
        runner_source="default",
        runner_fallback="mise->direct",
    )
    record = _parse_command_record(result)
    assert record["effective_runner"] == "direct"
    assert record["runner_source"] == "default"
    assert record["runner_fallback"] == "mise->direct"


def test_build_command_record_omits_runner_info_when_none() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = _parse_command_record(result)
    assert "effective_runner" not in record
    assert "runner_source" not in record
    assert "runner_fallback" not in record


def test_build_command_record_omits_runner_info_when_path_override() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["/custom/mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        effective_runner="direct",
        runner_source="path-override",
    )
    record = _parse_command_record(result)
    assert "effective_runner" not in record
    assert "runner_source" not in record
    assert "runner_fallback" not in record


def test_build_command_record_cached_includes_cached_from() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        formatter_failed=False,
        files=3,
        output="",
        elapsed=1.23,
        cached=True,
        cached_from="01ABCDEFGH",
    )
    record = _parse_command_record(result)
    assert record["cached"] is True
    assert record["cached_from"] == "01ABCDEFGH"
    assert "elapsed" not in record
    assert record["cached_elapsed"] == 1.23


def test_build_command_record_cached_omitted_when_false() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        formatter_failed=False,
        files=3,
        output="",
        elapsed=0.5,
    )
    record = _parse_command_record(result)
    assert "cached" not in record
    assert "cached_elapsed" not in record
    assert record["elapsed"] == 0.5
    assert "cached_from" not in record


def test_build_command_record_cached_without_cached_from() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        formatter_failed=False,
        files=3,
        output="",
        elapsed=2.0,
        cached=True,
    )
    record = _parse_command_record(result)
    assert record["cached"] is True
    assert "cached_from" not in record
    assert "elapsed" not in record
    assert record["cached_elapsed"] == 2.0


def test_build_command_record_message_truncated_when_archived() -> None:
    head_marker = "HEAD-MARKER-LINE"
    tail_marker = "TAIL-MARKER-LINE"
    body = "\n".join(f"line{i}" for i in range(4000))
    output = head_marker + "\n" + body + "\n" + tail_marker
    result = pyfltr.command.core_.CommandResult(
        command="shellcheck",
        command_type="linter",
        commandline=["shellcheck"],
        returncode=1,
        files=1,
        output=output,
        elapsed=0.1,
        archived=True,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_command_lines(result, config)
    record = json.loads(lines[-1])
    assert "message" in record
    message = record["message"]
    assert message.startswith(head_marker)
    assert message.endswith(tail_marker)
    assert "... (truncated)" in message
    truncated = record["truncated"]
    assert truncated["archive"] == "tools/shellcheck/output.log"
    assert truncated["chars"] == len(output)
    assert truncated["head_chars"] > 0
    assert truncated["tail_chars"] > 0
    assert truncated["head_chars"] + truncated["tail_chars"] <= 2000


def test_build_header_record_emits_commands_and_no_schema_hints() -> None:
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [],
        config,
        exit_code=0,
        commands=["ruff-check", "mypy", "textlint"],
        files=3,
        run_id="01TESTULID",
    )
    header_line = next(line for line in lines if json.loads(line).get("kind") == "header")
    record = json.loads(header_line)
    assert record["run_id"] == "01TESTULID"
    assert record["commands"] == ["ruff-check", "mypy", "textlint"]
    assert "commands_count" not in record
    assert "schema_hints" not in record


def test_build_header_record_size_is_small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uv_available", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uvx_available", lambda: True)
    monkeypatch.setattr(
        "pyfltr.output.jsonl_records.sys.version",
        "3.14.4 (tags/v3.14.4:23116f9, Apr  7 2026, 14:10:54) [MSC v.1944 64 bit (AMD64)]",
    )
    monkeypatch.setattr("pyfltr.output.jsonl_records.sys.executable", r"D:\a\pyfltr\pyfltr\.venv\Scripts\python.exe")
    monkeypatch.setattr("pyfltr.output.jsonl_records.os.getcwd", lambda: r"D:\a\pyfltr\pyfltr")
    monkeypatch.setattr("pyfltr.output.jsonl_records.importlib.metadata.version", lambda _name: "0.1.dev1+g0123456789")
    commands = [f"tool-{i}" for i in range(15)]
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [],
        config,
        exit_code=0,
        commands=commands,
        files=10,
        run_id="01TESTULID",
    )
    header_line = next(line for line in lines if json.loads(line).get("kind") == "header")
    assert len(header_line) <= 500, f"header size {len(header_line)} exceeded 500 chars"


def test_build_header_record_includes_uv_object(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uv_available", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uvx_available", lambda: True)
    config = pyfltr.config.config.create_default_config()

    lines_python = pyfltr.output.jsonl.build_lines([], config, exit_code=0, commands=["mypy"], files=3)
    header_python = json.loads(next(line for line in lines_python if json.loads(line).get("kind") == "header"))
    assert header_python["uv"] == {"lock": True, "available": True, "x_available": True}

    lines_non_python = pyfltr.output.jsonl.build_lines([], config, exit_code=0, commands=["shellcheck"], files=3)
    header_non_python = json.loads(next(line for line in lines_non_python if json.loads(line).get("kind") == "header"))
    assert header_non_python["uv"] == {"lock": True, "available": True, "x_available": True}


def test_build_header_record_uv_fields_reflect_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda: False)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uv_available", lambda: False)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uvx_available", lambda: False)
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([], config, exit_code=0, commands=["mypy"], files=3)
    header_line = next(line for line in lines if json.loads(line).get("kind") == "header")
    record = json.loads(header_line)
    assert record["uv"] == {"lock": False, "available": False, "x_available": False}


def test_build_header_record_omits_mise_active_tools_when_no_mise_command() -> None:
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([], config, exit_code=0, commands=["mypy", "ruff-check"], files=3)
    header_line = next(line for line in lines if json.loads(line).get("kind") == "header")
    record = json.loads(header_line)
    assert "mise_active_tools" not in record


def test_build_header_record_includes_mise_active_tools_when_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(status="ok", tools={"rust": []}),
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([], config, exit_code=0, commands=["cargo-fmt"], files=3)
    header_line = next(line for line in lines if json.loads(line).get("kind") == "header")
    record = json.loads(header_line)
    assert record["mise_active_tools"]["status"] == "ok"
    assert record["mise_active_tools"]["active_keys"] == ["rust"]


def test_collect_mise_active_tools_for_header_skips_when_no_mise_command() -> None:
    config = pyfltr.config.config.create_default_config()
    info = pyfltr.output.jsonl_records.collect_mise_active_tools_for_header(["mypy", "ruff-check"], config)
    assert info is None


def test_collect_mise_active_tools_for_header_includes_when_mise_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(status="ok", tools={"rust": []}),
    )
    config = pyfltr.config.config.create_default_config()
    info = pyfltr.output.jsonl_records.collect_mise_active_tools_for_header(["cargo-fmt"], config)
    assert info == {"status": "ok", "active_keys": ["rust"]}


def test_collect_mise_active_tools_for_header_propagates_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="untrusted-no-side-effects", detail="config not trusted"
        ),
    )
    config = pyfltr.config.config.create_default_config()
    info = pyfltr.output.jsonl_records.collect_mise_active_tools_for_header(["cargo-fmt"], config)
    assert info == {"status": "untrusted-no-side-effects", "detail": "config not trusted"}


def test_build_summary_record_emits_guidance_on_failure() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [result],
        config,
        exit_code=1,
        run_id="01JABCDEFGH",
        launcher_prefix=["uvx", "pyfltr"],
    )
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    assert guidance
    joined = " ".join(guidance)
    assert "retry_command" in joined
    assert "uvx pyfltr run --only-failed" in joined
    assert "uvx pyfltr show-run 01JABCDEFGH" in joined
    assert "<run_id>" not in joined
    assert "tool-resolve" not in joined


def test_build_summary_record_guidance_leads_with_resolution_failure_action() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=[],
        returncode=1,
        files=1,
        output="",
        elapsed=0.0,
        resolution_failed=True,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=1, run_id="01JABCDEFGH")
    record = json.loads(next(line for line in lines if json.loads(line).get("kind") == "summary"))
    assert "tool-resolve" in record["guidance"][0]


def test_build_summary_record_guidance_without_archive() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=1)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    joined = " ".join(guidance)
    assert "command.retry_command" in joined
    assert "diagnostic.fix" in joined
    assert "show-run" not in joined
    assert "--only-failed" not in joined
    assert "<run_id>" not in joined


def test_build_summary_record_guidance_uses_given_subcommand() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [result],
        config,
        exit_code=1,
        launcher_prefix=["uvx", "pyfltr"],
        subcommand="ci",
        run_id="01JARCHIVE",
    )
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    joined = " ".join(json.loads(summary_line)["guidance"])
    assert "uvx pyfltr ci --only-failed" in joined


def test_build_summary_record_counts_resolution_failed() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="shellcheck",
        command_type="linter",
        commandline=[],
        returncode=1,
        files=2,
        output="ツールが見つかりません",
        elapsed=0.0,
        resolution_failed=True,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=1)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    assert record["commands_summary"]["needs_action"]["failed"] == 0
    assert record["commands_summary"]["needs_action"]["resolution_failed"] == 1
    assert "guidance" in record


def test_build_summary_record_groups_statuses_into_no_issues_and_needs_action() -> None:
    succeeded = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    formatted = pyfltr.command.core_.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff", "format"],
        returncode=1,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    skipped = pyfltr.command.core_.CommandResult(
        command="pylint",
        command_type="linter",
        commandline=["pylint"],
        returncode=None,
        formatter_failed=False,
        files=0,
        output="",
        elapsed=0.0,
    )
    failed = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff", "check"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.0,
    )
    resolution_failed = pyfltr.command.core_.CommandResult(
        command="shellcheck",
        command_type="linter",
        commandline=[],
        returncode=1,
        files=1,
        output="",
        elapsed=0.0,
        resolution_failed=True,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [succeeded, formatted, skipped, failed, resolution_failed],
        config,
        exit_code=1,
    )
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    commands_summary = record["commands_summary"]
    assert commands_summary["no_issues"] == {"succeeded": 1, "formatted": 1, "skipped": 1}
    assert commands_summary["needs_action"]["failed"] == 1
    assert commands_summary["needs_action"]["resolution_failed"] == 1
    assert "no_issues" not in record
    assert "needs_action" not in record
    assert "succeeded" not in record
    assert "formatted" not in record
    assert "failed" not in record
    assert "resolution_failed" not in record
    assert "skipped" not in record


def test_build_summary_record_field_order_and_total_under_commands_summary() -> None:
    failed = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.0,
    )
    succeeded = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff", "check"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([failed, succeeded], config, exit_code=1)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    keys = list(record.keys())
    assert keys.index("kind") < keys.index("exit") < keys.index("commands_summary") < keys.index("diagnostics")
    assert "total" not in record
    commands_summary = record["commands_summary"]
    cs_keys = list(commands_summary.keys())
    assert cs_keys.index("no_issues") < cs_keys.index("needs_action") < cs_keys.index("total")
    assert commands_summary["total"] == 2


def test_build_summary_record_omits_resolution_failed_when_zero() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.0,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=1)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    needs_action = record["commands_summary"]["needs_action"]
    assert needs_action["failed"] == 1
    assert "resolution_failed" not in needs_action


def test_build_summary_record_failed_always_present() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    needs_action = record["commands_summary"]["needs_action"]
    assert needs_action["failed"] == 0
    assert "resolution_failed" not in needs_action


def test_build_summary_record_no_guidance_on_success() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    assert "guidance" not in record


def test_build_summary_record_guidance_emits_formatter_notice_only() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff", "format"],
        returncode=1,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        fixed_files=["src/a.py"],
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    assert len(guidance) == 1
    assert "formatter/fix-stage rewrote files" in guidance[0]
    assert "re-running is not required" in guidance[0]


def test_build_summary_record_guidance_combines_failure_and_formatter_notice() -> None:
    failed = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
    )
    formatted = pyfltr.command.core_.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff", "format"],
        returncode=1,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        fixed_files=["src/a.py"],
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [failed, formatted],
        config,
        exit_code=1,
        run_id="01JABCDEFGH",
        launcher_prefix=["pyfltr"],
    )
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    assert len(guidance) == 5
    assert "retry_command" in guidance[0]
    assert "formatter/fix-stage rewrote files" in guidance[-1]


def test_build_summary_record_includes_fully_excluded_files() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [result],
        config,
        exit_code=0,
        fully_excluded_files=["docs/ignored.md", "src/also.py"],
    )
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    assert record["fully_excluded_files"] == ["docs/ignored.md", "src/also.py"]


def test_build_summary_record_omits_fully_excluded_files_when_empty() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    values: list[list[str] | None] = [None, []]
    for value in values:
        lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0, fully_excluded_files=value)
        summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
        record = json.loads(summary_line)
        assert "fully_excluded_files" not in record


def test_build_summary_record_includes_missing_targets() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines(
        [result],
        config,
        exit_code=1,
        missing_targets=["does_not_exist.py", "also_missing.md"],
        fully_excluded_files=["docs/excluded.md"],
    )
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    assert record["missing_targets"] == ["does_not_exist.py", "also_missing.md"]
    assert record["fully_excluded_files"] == ["docs/excluded.md"]


def test_build_summary_record_omits_missing_targets_when_empty() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    values: list[list[str] | None] = [None, []]
    for value in values:
        lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0, missing_targets=value)
        summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
        record = json.loads(summary_line)
        assert "missing_targets" not in record


def test_build_message_dict_includes_end_line_and_end_col() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="a.md",
        line=17,
        col=1,
        command="textlint",
        message="Long sentence (L17:1〜23)",
        rule="ja-technical-writing/sentence-length",
        end_line=17,
        end_col=23,
    )
    records, _, _ = pyfltr.output.jsonl_records.aggregate_diagnostics([error])
    record = records[0]["messages"][0]
    assert record["end_line"] == 17
    assert record["end_col"] == 23
    keys = list(record.keys())
    assert keys.index("col") < keys.index("end_line") < keys.index("end_col") < keys.index("rule")


def test_build_message_dict_omits_end_line_and_end_col_when_none() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="a.py",
        line=10,
        col=5,
        command="mypy",
        message="x",
    )
    records, _, _ = pyfltr.output.jsonl_records.aggregate_diagnostics([error])
    record = records[0]["messages"][0]
    assert "end_line" not in record
    assert "end_col" not in record


def test_build_message_dict_omits_hint() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="a.md",
        line=1,
        col=1,
        command="textlint",
        message="文が長すぎます",
        rule="ja-technical-writing/sentence-length",
        hint="Split with periods to shorten.",
    )
    records, _, _ = pyfltr.output.jsonl_records.aggregate_diagnostics([error])
    record = records[0]["messages"][0]
    assert "hint" not in record


def test_build_command_record_includes_hints_from_errors() -> None:
    errors = [
        pyfltr.diagnostics.ErrorLocation(
            file="a.md",
            line=1,
            col=1,
            command="textlint",
            message="長い文です",
            rule="ja-technical-writing/sentence-length",
            hint=(
                "textlint counts up to the period (。) as one sentence;"
                " bullet-line splits still count as one."
                " Split with periods to shorten."
            ),
        ),
        pyfltr.diagnostics.ErrorLocation(
            file="a.md",
            line=5,
            col=1,
            command="textlint",
            message="また長い文です",
            rule="ja-technical-writing/sentence-length",
            hint=(
                "textlint counts up to the period (。) as one sentence;"
                " bullet-line splits still count as one."
                " Split with periods to shorten."
            ),
        ),
    ]
    _, _, hints = pyfltr.output.jsonl_records.aggregate_diagnostics(errors)
    assert hints == {
        "ja-technical-writing/sentence-length": (
            "textlint counts up to the period (。) as one sentence;"
            " bullet-line splits still count as one."
            " Split with periods to shorten."
        )
    }


def test_build_command_record_hints_key_present_when_hints_given() -> None:
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py",
        line=1,
        col=None,
        command="ruff-check",
        message="unused import",
        rule="F401",
        hint="Remove unused import.",
    )
    result = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        errors=[error],
    )
    record = _parse_command_record(result)
    assert record["hints"] == {"F401": "Remove unused import."}


def test_build_command_record_hints_key_omitted_when_empty() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = _parse_command_record(result)
    assert "hints" not in record


def test_build_command_record_textlint_col_hint_only_when_diagnostics() -> None:
    result_no_diag = pyfltr.command.core_.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
    )
    record_no_diag = _parse_command_record(result_no_diag)
    assert "hints" not in record_no_diag

    error_with_hint = pyfltr.diagnostics.ErrorLocation(
        file="a.md",
        line=1,
        col=1,
        command="textlint",
        message="文が長すぎます",
        rule="ja-technical-writing/sentence-length",
        hint=(
            "textlint counts up to the period (。) as one sentence;"
            " bullet-line splits still count as one."
            " Split with periods to shorten."
        ),
    )
    result_with_diag = pyfltr.command.core_.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        errors=[error_with_hint],
    )
    record_with_hints = _parse_command_record(result_with_diag)
    assert "messages[].col" in record_with_hints["hints"]
    assert "messages[].end_col" not in record_with_hints["hints"]
    col_hint = record_with_hints["hints"]["messages[].col"]
    assert "col" in col_hint and "end_col" in col_hint
    assert "ja-technical-writing/sentence-length" in record_with_hints["hints"]
    assert "ja-technical-writing/max-comma" not in record_with_hints["hints"]


def test_build_summary_record_includes_applied_fixes() -> None:
    result_a = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="formatter",
        commandline=["ruff"],
        returncode=1,
        formatter_failed=False,
        files=2,
        output="",
        elapsed=0.1,
        fixed_files=["src/b.py", "src/a.py"],
    )
    result_b = pyfltr.command.core_.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff"],
        returncode=1,
        formatter_failed=False,
        files=2,
        output="",
        elapsed=0.1,
        fixed_files=["src/a.py", "src/c.py"],
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result_a, result_b], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    assert record["applied_fixes"] == ["src/a.py", "src/b.py", "src/c.py"]


def test_build_summary_record_normalizes_applied_fixes_separators() -> None:
    target = pathlib.Path(r"src\nested\fixed.py")
    fixed_files = pyfltr.command.snapshot.changed_files({target: b"before"}, {target: b"after"})
    result = pyfltr.command.core_.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff"],
        returncode=1,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
        fixed_files=fixed_files,
    )

    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)

    assert record["applied_fixes"] == ["src/nested/fixed.py"]


def test_build_summary_record_omits_applied_fixes_when_empty() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    assert "applied_fixes" not in record


def test_build_summary_record_counts_warning() -> None:
    warning_result = pyfltr.command.core_.CommandResult(
        command="colloquial",
        command_type="linter",
        commandline=["uv"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        severity="warning",
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([warning_result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    needs_action = record["commands_summary"]["needs_action"]
    assert needs_action["failed"] == 0
    assert needs_action["warning"] == 1


def test_build_summary_record_warning_count_always_present() -> None:
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    needs_action = record["commands_summary"]["needs_action"]
    assert needs_action["failed"] == 0
    assert needs_action["warning"] == 0


def test_build_summary_record_no_failure_guidance_when_only_warning() -> None:
    warning_result = pyfltr.command.core_.CommandResult(
        command="colloquial",
        command_type="linter",
        commandline=["uv"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        severity="warning",
    )
    config = pyfltr.config.config.create_default_config()
    lines = pyfltr.output.jsonl.build_lines([warning_result], config, exit_code=0)
    summary_line = next(line for line in lines if json.loads(line).get("kind") == "summary")
    record = json.loads(summary_line)
    assert "guidance" not in record


def test_build_command_record_includes_user_hints_when_diagnostics_present() -> None:
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-hints"] = ["First user hint.", "Second user hint."]
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py",
        line=1,
        col=None,
        command="mypy",
        message="error msg",
    )
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        files=1,
        output="",
        elapsed=0.1,
        errors=[error, error],
    )
    lines = pyfltr.output.jsonl.build_command_lines(result, config)
    record = json.loads(lines[-1])
    hints = record.get("hints", {})
    assert hints.get("user.0") == "First user hint."
    assert hints.get("user.1") == "Second user hint."


def test_build_command_record_omits_user_hints_when_no_diagnostics() -> None:
    config = pyfltr.config.config.create_default_config()
    config.values["mypy-hints"] = ["This must not be emitted."]
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    lines = pyfltr.output.jsonl.build_command_lines(result, config)
    record = json.loads(lines[-1])
    hints = record.get("hints", {})
    assert all(not key.startswith("user.") for key in hints)


def test_summary_completion_fields(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    (work_dir / "sample.txt").write_text("value\n", encoding="utf-8")
    (work_dir / "pyproject.toml").write_text(
        "[tool.pyfltr]\nrespect-gitignore = false\n\n"
        "[tool.pyfltr.custom-commands.always-ok]\n"
        f"path = {json.dumps(sys.executable)}\n"
        'args = ["-c", "pass"]\ntype = "linter"\ntargets = ["*.txt"]\npass-filenames = false\n',
        encoding="utf-8",
    )

    def run_summary(target: pathlib.Path) -> dict:
        destination = tmp_path / f"{target.stem}.jsonl"
        pyfltr.cli.main.run(
            [
                "run",
                "--work-dir",
                str(work_dir),
                "--output-format=jsonl",
                f"--output-file={destination}",
                "--commands=always-ok",
                "--no-cache",
                str(target),
            ]
        )
        return json.loads(destination.read_text(encoding="utf-8").splitlines()[-1])

    summary = run_summary(work_dir / "sample.txt")
    assert list(summary)[:7] == [
        "kind",
        "exit",
        "completion",
        "files_reached",
        "completed_commands",
        "incomplete_commands",
        "commands_summary",
    ]
    assert summary["completion"] == "completed"
    assert summary["files_reached"] == 1
    assert summary["completed_commands"] == ["always-ok"]
    assert not summary["incomplete_commands"]

    early_exit = run_summary(work_dir / "missing.txt")
    assert early_exit["completion"] == "not_reached"
    assert early_exit["files_reached"] == 0
    assert not early_exit["completed_commands"]
    assert not early_exit["incomplete_commands"]
