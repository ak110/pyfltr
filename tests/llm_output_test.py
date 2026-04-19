"""llm_outputのテストコード。"""
# pylint: disable=protected-access,duplicate-code

import json

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.llm_output


def test_build_message_dict_with_rule_severity_fix() -> None:
    """rule・severity・fixフィールドがmessage dictに含まれることのテスト。"""
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
    message = pyfltr.llm_output._build_message_dict(error)
    assert message["line"] == 10
    assert message["col"] == 5
    assert message["rule"] == "F401"
    assert message["severity"] == "error"
    assert message["fix"] == "safe"
    assert message["msg"] == "`os` imported but unused"

    # msgは最後のキーであることを確認（フィールド順序）
    keys = list(message.keys())
    assert keys[-1] == "msg"
    # ``rule_url`` は出力されない
    assert "rule_url" not in message


def test_build_message_dict_none_fields_omitted() -> None:
    """col・rule・severity・fixがNoneのときフィールドが省略されることのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=None,
        command="mypy",
        message="Name 'x' is not defined",
    )
    message = pyfltr.llm_output._build_message_dict(error)
    assert "col" not in message
    assert "rule" not in message
    assert "severity" not in message
    assert "fix" not in message
    assert message["msg"] == "Name 'x' is not defined"


def test_build_message_dict_partial_fields() -> None:
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
    message = pyfltr.llm_output._build_message_dict(error)
    assert message["rule"] == "C0114"
    assert message["severity"] == "warning"
    assert "fix" not in message


def test_aggregate_diagnostics_groups_by_tool_and_file() -> None:
    """同一tool×fileの指摘が1レコードに集約され、messages[]が(line, col, rule)順に並ぶ。"""
    errors = [
        pyfltr.error_parser.ErrorLocation(file="src/a.py", line=10, col=3, command="ruff-check", message="msg10b", rule="E501"),
        pyfltr.error_parser.ErrorLocation(file="src/a.py", line=10, col=3, command="ruff-check", message="msg10a", rule="E401"),
        pyfltr.error_parser.ErrorLocation(file="src/a.py", line=5, col=None, command="ruff-check", message="msg5"),
        pyfltr.error_parser.ErrorLocation(file="src/b.py", line=1, col=None, command="ruff-check", message="msgB"),
    ]
    records, hint_urls = pyfltr.llm_output.aggregate_diagnostics(errors)
    assert len(records) == 2
    assert records[0]["tool"] == "ruff-check"
    assert records[0]["file"] == "src/a.py"
    assert [m.get("rule") for m in records[0]["messages"]] == [None, "E401", "E501"]
    assert [m["line"] for m in records[0]["messages"]] == [5, 10, 10]
    assert records[1]["file"] == "src/b.py"
    assert not hint_urls


def test_aggregate_diagnostics_collects_hint_urls() -> None:
    """rule_url付きのerrorsからhint_urls辞書が構築される。"""
    errors = [
        pyfltr.error_parser.ErrorLocation(
            file="a.py",
            line=1,
            col=None,
            command="ruff-check",
            message="m1",
            rule="F401",
            rule_url="https://docs.astral.sh/ruff/rules/F401/",
        ),
        pyfltr.error_parser.ErrorLocation(
            file="b.py",
            line=2,
            col=None,
            command="ruff-check",
            message="m2",
            rule="F401",
            rule_url="https://docs.astral.sh/ruff/rules/F401/",
        ),
        pyfltr.error_parser.ErrorLocation(
            file="a.py",
            line=3,
            col=None,
            command="ruff-check",
            message="m3",
            rule="E501",
        ),
    ]
    _, hint_urls = pyfltr.llm_output.aggregate_diagnostics(errors)
    assert hint_urls == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


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
    records, _ = pyfltr.llm_output.aggregate_diagnostics([error])
    line = pyfltr.llm_output._dump(records[0])
    parsed = json.loads(line)
    assert parsed["kind"] == "diagnostic"
    assert parsed["tool"] == "ruff-check"
    assert parsed["messages"][0]["rule"] == "F401"


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


def test_build_tool_record_includes_hint_urls_when_provided() -> None:
    """hint_urls を与えると tool レコードにハイフン区切りキーで埋め込まれる。"""
    result = pyfltr.command.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.llm_output._build_tool_record(
        result,
        diagnostics=1,
        hint_urls={"F401": "https://docs.astral.sh/ruff/rules/F401/"},
    )
    assert record["hint-urls"] == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_build_tool_record_omits_hint_urls_when_empty() -> None:
    """hint_urls が None / 空の場合は `hint-urls` キー自体を出さない。"""
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
    record_none = pyfltr.llm_output._build_tool_record(result, diagnostics=0, hint_urls=None)
    record_empty = pyfltr.llm_output._build_tool_record(result, diagnostics=0, hint_urls={})
    assert "hint-urls" not in record_none
    assert "hint-urls" not in record_empty


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
    """jsonl-diagnostic-limit 超過時、先頭 N 件の個別指摘に切り詰めてから集約する。"""
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
    # 3件とも同一fileのため集約後は1 diagnostic行 + tool行 = 2行
    assert len(lines) == 2
    diag_record = json.loads(lines[0])
    assert diag_record["kind"] == "diagnostic"
    assert len(diag_record["messages"]) == 3
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
    # 切り詰めなし: 同一fileのため集約後は1 diagnostic行 + tool行 = 2行、messages 10件
    assert len(lines) == 2
    diag_record = json.loads(lines[0])
    assert len(diag_record["messages"]) == 10
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
    assert "diagnostic.messages" in hints
    assert "diagnostic.messages.fix" in hints
    assert "tool.hint-urls" in hints
    assert "tool.retry_command" in hints
    assert "header.run_id" in hints
    # 集約形式以降、rule_url はトップレベルキーから削除されている
    assert "diagnostic.rule_url" not in hints
    # 値は英語で LLM が読む前提
    assert "auto-fix" in hints["diagnostic.messages.fix"]


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


def test_build_summary_record_includes_fully_excluded_files() -> None:
    """fully_excluded_files 指定時は summary レコードに出力される。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.llm_output._build_summary_record(
        [result],
        exit_code=0,
        fully_excluded_files=["docs/ignored.md", "src/also.py"],
    )
    assert record["fully_excluded_files"] == ["docs/ignored.md", "src/also.py"]


def test_build_summary_record_omits_fully_excluded_files_when_empty() -> None:
    """空リスト・None の場合はキー自体を出力しない。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    values: list[list[str] | None] = [None, []]
    for value in values:
        record = pyfltr.llm_output._build_summary_record([result], exit_code=0, fully_excluded_files=value)
        assert "fully_excluded_files" not in record


def test_build_message_dict_includes_hint() -> None:
    """ErrorLocation.hint が非 None なら messages[] に含まれる。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="a.md",
        line=1,
        col=1,
        command="textlint",
        message="文が長すぎます",
        rule="ja-technical-writing/sentence-length",
        hint="句点で文を区切る",
    )
    record = pyfltr.llm_output._build_message_dict(error)
    assert record["hint"] == "句点で文を区切る"


def test_build_message_dict_omits_hint_when_none() -> None:
    """hint が None なら messages[] には含まれない。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="a.py",
        line=1,
        col=1,
        command="mypy",
        message="x",
    )
    record = pyfltr.llm_output._build_message_dict(error)
    assert "hint" not in record
