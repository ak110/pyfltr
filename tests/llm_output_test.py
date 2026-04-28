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
    # `rule_url`は出力されない
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
    assert records[0]["command"] == "ruff-check"
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
    assert parsed["command"] == "ruff-check"
    assert parsed["messages"][0]["rule"] == "F401"


def test_build_warning_record() -> None:
    """warning dictがkind/source/msgを持つレコードに変換される。"""
    record = pyfltr.llm_output._build_warning_record({"source": "config", "message": "foo"})
    assert record == {"kind": "warning", "source": "config", "msg": "foo"}


def test_build_warning_record_with_hint() -> None:
    """hintがあればwarningレコードにhintキーが含まれる。"""
    record = pyfltr.llm_output._build_warning_record(
        {"source": "textlint-identifier-corruption", "message": "foo", "hint": "fooをバックティックで囲む"}
    )
    assert record == {
        "kind": "warning",
        "source": "textlint-identifier-corruption",
        "msg": "foo",
        "hint": "fooをバックティックで囲む",
    }


def test_build_command_record_includes_hint_urls_when_provided() -> None:
    """hint_urlsを与えるとtoolレコードに`hint_urls`キーで埋め込まれる。"""
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
    record = pyfltr.llm_output._build_command_record(
        result,
        diagnostics=1,
        hint_urls={"F401": "https://docs.astral.sh/ruff/rules/F401/"},
    )
    assert record["hint_urls"] == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_build_command_record_omits_hint_urls_when_empty() -> None:
    """hint_urlsがNone / 空の場合は`hint_urls`キー自体を出さない。"""
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
    record_none = pyfltr.llm_output._build_command_record(result, diagnostics=0, hint_urls=None)
    record_empty = pyfltr.llm_output._build_command_record(result, diagnostics=0, hint_urls={})
    assert "hint_urls" not in record_none
    assert "hint_urls" not in record_empty


def test_build_command_record_retry_command_included() -> None:
    """retry_commandが設定されていればtoolレコードに含まれる（失敗時のみpopulateされる前提）。"""
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
    record = pyfltr.llm_output._build_command_record(result, diagnostics=0)
    assert record["retry_command"] == "pyfltr run --commands ruff-check -- src/foo.py"


def test_build_command_record_retry_command_omitted() -> None:
    """retry_commandがNoneの場合、toolレコードから省略される。"""
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
    record = pyfltr.llm_output._build_command_record(result, diagnostics=0)
    assert "retry_command" not in record


def test_build_command_lines_truncates_diagnostics_when_archived() -> None:
    """jsonl-diagnostic-limit超過時、先頭N件の個別指摘に切り詰めてから集約する。"""
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
    lines = pyfltr.llm_output.build_command_lines(result, config)
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
    lines = pyfltr.llm_output.build_command_lines(result, config)
    # 切り詰めなし: 同一fileのため集約後は1 diagnostic行 + tool行 = 2行、messages 10件
    assert len(lines) == 2
    diag_record = json.loads(lines[0])
    assert len(diag_record["messages"]) == 10
    tool_record = json.loads(lines[-1])
    assert tool_record["diagnostics"] == 10
    assert "truncated" not in tool_record


def test_build_command_record_cached_includes_cached_from() -> None:
    """cached=Trueのときcached/cached_fromとcached_elapsedがtoolレコードに含まれる。"""
    result = pyfltr.command.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        has_error=False,
        files=3,
        output="",
        elapsed=1.23,
        cached=True,
        cached_from="01ABCDEFGH",
    )
    record = pyfltr.llm_output._build_command_record(result, diagnostics=0)
    assert record["cached"] is True
    assert record["cached_from"] == "01ABCDEFGH"
    # cached=Trueのときelapsedはださずcached_elapsedだけを出す
    # （LLMが「今回の実行時間」と誤解するのを避ける）。
    assert "elapsed" not in record
    assert record["cached_elapsed"] == 1.23


def test_build_command_record_cached_omitted_when_false() -> None:
    """cached=Falseの場合はcached/cached_from/cached_elapsedが省略されelapsedが出る。"""
    result = pyfltr.command.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        has_error=False,
        files=3,
        output="",
        elapsed=0.5,
    )
    record = pyfltr.llm_output._build_command_record(result, diagnostics=0)
    assert "cached" not in record
    assert "cached_elapsed" not in record
    assert record["elapsed"] == 0.5
    assert "cached_from" not in record


def test_build_command_record_cached_without_cached_from() -> None:
    """cached_fromが未設定でもcached=Trueならcached_elapsedは出る。"""
    result = pyfltr.command.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        has_error=False,
        files=3,
        output="",
        elapsed=2.0,
        cached=True,
    )
    record = pyfltr.llm_output._build_command_record(result, diagnostics=0)
    assert record["cached"] is True
    assert "cached_from" not in record
    assert "elapsed" not in record
    assert record["cached_elapsed"] == 2.0


def test_build_command_record_message_truncated_when_archived() -> None:
    """failed + message切り詰め時、truncatedにlines / chars / head_chars / tail_chars / archiveが入る。

    ハイブリッド方式の検証:
    - 先頭ブロックは原文先頭の文字を保持する
    - 末尾ブロックは原文末尾の文字を保持する
    - 中央に`... (truncated)`マーカーが入る
    """
    # 既定上限（max_chars=2000）を確実に超える4000行 + マーカー文字列を仕込む。
    head_marker = "HEAD-MARKER-LINE"
    tail_marker = "TAIL-MARKER-LINE"
    body = "\n".join(f"line{i}" for i in range(4000))
    output = head_marker + "\n" + body + "\n" + tail_marker
    result = pyfltr.command.CommandResult(
        command="shellcheck",
        command_type="linter",
        commandline=["shellcheck"],
        returncode=1,
        has_error=True,
        files=1,
        output=output,
        elapsed=0.1,
        archived=True,
    )
    config = pyfltr.config.create_default_config()
    record = pyfltr.llm_output._build_command_record(result, diagnostics=0, config=config)
    assert "message" in record
    message = record["message"]
    # 先頭ブロックは原文の冒頭をそのまま保持する。
    assert message.startswith(head_marker)
    # 末尾ブロックは原文の末尾を保持する。
    assert message.endswith(tail_marker)
    # 中央に切り詰めマーカーが入る。
    assert "... (truncated)" in message
    truncated = record["truncated"]
    assert truncated["archive"] == "tools/shellcheck/output.log"
    assert truncated["chars"] == len(output)
    assert truncated["head_chars"] > 0
    assert truncated["tail_chars"] > 0
    # 合計（head + tail + marker）はmax_charsを大きくは超えない。
    assert truncated["head_chars"] + truncated["tail_chars"] <= 2000


def test_build_header_record_default_compact() -> None:
    """既定ではcommands配列と短縮schema_hintsを出す。"""
    record = pyfltr.llm_output._build_header_record(commands=["ruff-check", "mypy", "textlint"], files=3, run_id="01TESTULID")
    assert record["run_id"] == "01TESTULID"
    # 実行対象ツール名の配列として出す。commands_countは廃止済み。
    assert record["commands"] == ["ruff-check", "mypy", "textlint"]
    assert "commands_count" not in record
    hints = record.get("schema_hints")
    assert isinstance(hints, dict)
    # 短縮版はLLMが推測しづらい項目だけを載せる。自明なレコード種別（diagnostic/warning/summary）は含まない。
    assert "header.commands_count" not in hints
    assert "command.cached_elapsed" in hints
    assert "command.retry_command" in hints
    assert "messages[].fix" in hints
    # 短縮版自身の使い方説明はトークン効率を下げるため埋め込まない（フル版の取得方法はドキュメントに委ねる）。
    assert "_note" not in hints
    # フル版固有のキーは出さない（同名衝突しないよう別体系）
    assert "diagnostic.messages" not in hints


def test_build_header_record_verbose_has_full_hints() -> None:
    """verbose=Trueでschema_hintsがフル版に切り替わる。commands配列は既定でも出る。"""
    record = pyfltr.llm_output._build_header_record(commands=["ruff-check"], files=3, run_id="01TESTULID", verbose=True)
    assert record["run_id"] == "01TESTULID"
    assert record["commands"] == ["ruff-check"]
    assert "commands_count" not in record
    hints = record.get("schema_hints")
    assert isinstance(hints, dict)
    assert "diagnostic.messages" in hints
    assert "diagnostic.messages.fix" in hints
    assert "command.hint_urls" in hints
    assert "command.retry_command" in hints
    assert "command.cached_elapsed" in hints
    assert "header.run_id" in hints
    # 集約形式以降、rule_urlはトップレベルキーから削除されている
    assert "diagnostic.rule_url" not in hints
    # 値は英語でLLMが読む前提
    assert "auto-fix" in hints["diagnostic.messages.fix"]


def test_build_header_record_size_default_is_small() -> None:
    """既定ヘッダー（実行対象15件想定）は900文字以下に収まる。"""
    # 想定: pyfltrの実運用上、実行対象は13件程度（有効化・only-failed適用後）。
    # 合成15件想定で900文字以内なら、実運用の~13件（平均10文字）は十分にその内側に収まる。
    commands = [f"tool-{i}" for i in range(15)]
    record = pyfltr.llm_output._build_header_record(commands=commands, files=10, run_id="01TESTULID")
    serialized = pyfltr.llm_output._dump(record)
    assert len(serialized) <= 900, f"header size {len(serialized)} exceeded 900 chars"


def test_build_lines_verbose_flag_switches_hints() -> None:
    """`build_lines`のverbose引数でheaderのschema_hintsが切り替わる（commandsは常に配列）。"""
    config = pyfltr.config.create_default_config()
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
    lines_default = pyfltr.llm_output.build_lines([result], config, exit_code=0, commands=["mypy", "ruff-check"], files=10)
    header_default = json.loads(lines_default[0])
    assert header_default["commands"] == ["mypy", "ruff-check"]
    assert "commands_count" not in header_default
    assert "_note" not in header_default["schema_hints"]
    assert "diagnostic.messages" not in header_default["schema_hints"]

    lines_verbose = pyfltr.llm_output.build_lines(
        [result], config, exit_code=0, commands=["mypy", "ruff-check"], files=10, verbose=True
    )
    header_verbose = json.loads(lines_verbose[0])
    assert header_verbose["commands"] == ["mypy", "ruff-check"]
    assert "commands_count" not in header_verbose
    assert "diagnostic.messages" in header_verbose["schema_hints"]


def test_get_schema_hints_public_api() -> None:
    """`get_schema_hints`はコピーを返し、full/compactで内容が切り替わる。"""
    full = pyfltr.llm_output.get_schema_hints(full=True)
    compact = pyfltr.llm_output.get_schema_hints(full=False)
    assert "diagnostic.messages" in full
    assert "diagnostic.messages" not in compact
    # 短縮版は使い方案内を含まない（フル版の取得方法はドキュメントに委ねる）。
    assert "_note" not in compact
    # コピーであること
    full["diagnostic.messages"] = "modified"
    assert pyfltr.llm_output.get_schema_hints(full=True)["diagnostic.messages"] != "modified"


def test_build_summary_record_emits_guidance_on_failure() -> None:
    """failed > 0のときsummary.guidanceが英語で付与され、launcher_prefixとrun_idが埋め込まれる。"""
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
    # プレースホルダーが残っていないこと
    assert "<run_id>" not in joined


def test_build_summary_record_guidance_falls_back_when_unspecified() -> None:
    """run_id / launcher_prefix未指定時はプレースホルダー・既定値にフォールバックする。"""
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


def test_build_summary_record_counts_resolution_failed() -> None:
    """resolution_failedはfailedと区別してcommands_summary.needs_action配下に集計され、guidanceも付与される。"""
    result = pyfltr.command.CommandResult(
        command="shellcheck",
        command_type="linter",
        commandline=[],
        returncode=1,
        has_error=True,
        files=2,
        output="ツールが見つかりません",
        elapsed=0.0,
        resolution_failed=True,
    )
    record = pyfltr.llm_output._build_summary_record([result], exit_code=1)
    assert record["commands_summary"]["needs_action"]["failed"] == 0
    assert record["commands_summary"]["needs_action"]["resolution_failed"] == 1
    assert "guidance" in record


def test_build_summary_record_groups_statuses_into_no_issues_and_needs_action() -> None:
    """5種別のステータスがcommands_summary.no_issues / needs_actionの2グループへ正しく振り分けられる。"""
    succeeded = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    formatted = pyfltr.command.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff", "format"],
        returncode=1,
        has_error=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    skipped = pyfltr.command.CommandResult(
        command="pylint",
        command_type="linter",
        commandline=["pylint"],
        returncode=None,
        has_error=False,
        files=0,
        output="",
        elapsed=0.0,
    )
    failed = pyfltr.command.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff", "check"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.0,
    )
    resolution_failed = pyfltr.command.CommandResult(
        command="shellcheck",
        command_type="linter",
        commandline=[],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.0,
        resolution_failed=True,
    )
    record = pyfltr.llm_output._build_summary_record(
        [succeeded, formatted, skipped, failed, resolution_failed],
        exit_code=1,
    )
    commands_summary = record["commands_summary"]
    assert commands_summary["no_issues"] == {"succeeded": 1, "formatted": 1, "skipped": 1}
    assert commands_summary["needs_action"] == {"failed": 1, "resolution_failed": 1}
    # 旧フラットキー・直下のグループキーは廃止されているため、トップレベルから消えていることも確認する。
    assert "no_issues" not in record
    assert "needs_action" not in record
    assert "succeeded" not in record
    assert "formatted" not in record
    assert "failed" not in record
    assert "resolution_failed" not in record
    assert "skipped" not in record


def test_build_summary_record_no_guidance_on_success() -> None:
    """failed == 0かつapplied_fixesも空のときはsummary.guidanceが省略される。"""
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


def test_build_summary_record_guidance_emits_formatter_notice_only() -> None:
    """failed/resolution_failed=0でもapplied_fixesが非空ならguidanceにformatter書き換え注記1項目だけ出る。"""
    result = pyfltr.command.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff", "format"],
        returncode=1,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
        fixed_files=["src/a.py"],
    )
    record = pyfltr.llm_output._build_summary_record([result], exit_code=0)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    assert len(guidance) == 1
    assert "formatter/fix-stage rewrote files" in guidance[0]
    assert "re-running is not required" in guidance[0]


def test_build_summary_record_guidance_combines_failure_and_formatter_notice() -> None:
    """failed>0かつapplied_fixes非空のときは失敗時の4項目に続けてformatter書き換え注記が並ぶ。"""
    failed = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    formatted = pyfltr.command.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff", "format"],
        returncode=1,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
        fixed_files=["src/a.py"],
    )
    record = pyfltr.llm_output._build_summary_record(
        [failed, formatted],
        exit_code=1,
        run_id="01JABCDEFGH",
        launcher_prefix=["pyfltr"],
    )
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    assert len(guidance) == 5
    assert "retry_command" in guidance[0]
    assert "formatter/fix-stage rewrote files" in guidance[-1]


def test_build_summary_record_includes_fully_excluded_files() -> None:
    """fully_excluded_files指定時はsummaryレコードに出力される。"""
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
    """空リスト・Noneの場合はキー自体を出力しない。"""
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


def test_build_message_dict_includes_end_line_and_end_col() -> None:
    """end_line / end_colが設定されていればmessages[]に出力される。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="a.md",
        line=17,
        col=1,
        command="textlint",
        message="Long sentence (L17:1〜23)",
        rule="ja-technical-writing/sentence-length",
        end_line=17,
        end_col=23,
    )
    record = pyfltr.llm_output._build_message_dict(error)
    assert record["end_line"] == 17
    assert record["end_col"] == 23
    # フィールド順はline → col → end_line → end_col → ruleの順
    keys = list(record.keys())
    assert keys.index("col") < keys.index("end_line") < keys.index("end_col") < keys.index("rule")


def test_build_message_dict_omits_end_line_and_end_col_when_none() -> None:
    """end_line / end_colがNoneの場合はキーごと省略する。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="a.py",
        line=10,
        col=5,
        command="mypy",
        message="x",
    )
    record = pyfltr.llm_output._build_message_dict(error)
    assert "end_line" not in record
    assert "end_col" not in record


def test_build_message_dict_includes_hint() -> None:
    """`ErrorLocation.hint`が非Noneならmessages[]に含まれる。"""
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
    """hintがNoneならmessages[]には含まれない。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="a.py",
        line=1,
        col=1,
        command="mypy",
        message="x",
    )
    record = pyfltr.llm_output._build_message_dict(error)
    assert "hint" not in record


def test_build_summary_record_includes_applied_fixes() -> None:
    """fixed_filesを持つ結果が複数ある場合、summary.applied_fixesにユニオンしてソートして出力される。

    構築意図: `returncode=1, has_error=False, command_type="formatter"`の組み合わせで
    `status == "formatted"`となるケースを再現する。
    2件の結果で`fixed_files`が重複を含む場合に、ユニオン＆ソートされた一覧が得られることを確認する。
    """
    result_a = pyfltr.command.CommandResult(
        command="ruff-check",
        command_type="formatter",
        commandline=["ruff"],
        returncode=1,
        has_error=False,
        files=2,
        output="",
        elapsed=0.1,
        fixed_files=["src/b.py", "src/a.py"],
    )
    result_b = pyfltr.command.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff"],
        returncode=1,
        has_error=False,
        files=2,
        output="",
        elapsed=0.1,
        fixed_files=["src/a.py", "src/c.py"],
    )
    record = pyfltr.llm_output._build_summary_record([result_a, result_b], exit_code=0)
    assert record["applied_fixes"] == ["src/a.py", "src/b.py", "src/c.py"]


def test_build_summary_record_omits_applied_fixes_when_empty() -> None:
    """fixed_filesが空のときsummary.applied_fixesは出力されない。"""
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
    assert "applied_fixes" not in record


def test_get_schema_hints_full_includes_applied_fixes() -> None:
    """フル版schema_hintsにsummary.applied_fixesの説明が含まれる。"""
    full = pyfltr.llm_output.get_schema_hints(full=True)
    assert "summary.applied_fixes" in full


def test_get_schema_hints_full_includes_summary_guidance() -> None:
    """フル版schema_hintsにsummary.guidanceの出力条件・内容説明が含まれる。"""
    full = pyfltr.llm_output.get_schema_hints(full=True)
    assert "summary.guidance" in full
    description = full["summary.guidance"]
    assert "needs_action" in description
    assert "applied_fixes" in description


def test_get_schema_hints_full_includes_summary_groups() -> None:
    """フル版schema_hintsにsummary.commands_summary配下の説明が含まれる。

    短縮版（`-v`無し）には含めず、kind構造から大意が推測できる前提とトークン消費の抑制方針を維持する。
    """
    full = pyfltr.llm_output.get_schema_hints(full=True)
    assert "summary.commands_summary" in full
    assert "summary.commands_summary.no_issues" in full
    assert "summary.commands_summary.needs_action" in full
    compact = pyfltr.llm_output.get_schema_hints(full=False)
    assert "summary.commands_summary" not in compact
    assert "summary.commands_summary.no_issues" not in compact
    assert "summary.commands_summary.needs_action" not in compact
