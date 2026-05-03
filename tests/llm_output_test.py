"""llm_outputのテストコード。"""
# pylint: disable=protected-access  # JSONL構造化レコード組み立てヘルパー（_build_*_record等）の単体テスト経路
# pylint: disable=duplicate-code  # 各レコードビルダー検証の組み立て手順が他テストと類似
# pylint: disable=too-many-lines  # JSONLビルダーの単体検証を本ファイルへ集約しているため

import json

import pytest

import pyfltr.command.core_
import pyfltr.command.error_parser
import pyfltr.command.mise
import pyfltr.config.config
import pyfltr.output.jsonl


def test_build_message_dict_with_rule_severity_fix() -> None:
    """rule・severity・fixフィールドがmessage dictに含まれることのテスト。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="`os` imported but unused",
        rule="F401",
        severity="error",
        fix="safe",
    )
    message = pyfltr.output.jsonl._build_message_dict(error)
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
    error = pyfltr.command.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=None,
        command="mypy",
        message="Name 'x' is not defined",
    )
    message = pyfltr.output.jsonl._build_message_dict(error)
    assert "col" not in message
    assert "rule" not in message
    assert "severity" not in message
    assert "fix" not in message
    assert message["msg"] == "Name 'x' is not defined"


def test_build_message_dict_partial_fields() -> None:
    """一部のフィールドのみ設定されている場合のテスト。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="pylint",
        message="Missing docstring",
        rule="C0114",
        severity="warning",
    )
    message = pyfltr.output.jsonl._build_message_dict(error)
    assert message["rule"] == "C0114"
    assert message["severity"] == "warning"
    assert "fix" not in message


def test_aggregate_diagnostics_groups_by_tool_and_file() -> None:
    """同一tool×fileの指摘が1レコードに集約され、messages[]が(line, col, rule)順に並ぶ。"""
    errors = [
        pyfltr.command.error_parser.ErrorLocation(
            file="src/a.py", line=10, col=3, command="ruff-check", message="msg10b", rule="E501"
        ),
        pyfltr.command.error_parser.ErrorLocation(
            file="src/a.py", line=10, col=3, command="ruff-check", message="msg10a", rule="E401"
        ),
        pyfltr.command.error_parser.ErrorLocation(file="src/a.py", line=5, col=None, command="ruff-check", message="msg5"),
        pyfltr.command.error_parser.ErrorLocation(file="src/b.py", line=1, col=None, command="ruff-check", message="msgB"),
    ]
    records, hint_urls, hints = pyfltr.output.jsonl.aggregate_diagnostics(errors)
    assert len(records) == 2
    assert records[0]["command"] == "ruff-check"
    assert records[0]["file"] == "src/a.py"
    assert [m.get("rule") for m in records[0]["messages"]] == [None, "E401", "E501"]
    assert [m["line"] for m in records[0]["messages"]] == [5, 10, 10]
    assert records[1]["file"] == "src/b.py"
    assert not hint_urls
    assert not hints


def test_aggregate_diagnostics_collects_hint_urls() -> None:
    """rule_url付きのerrorsからhint_urls辞書が構築される。"""
    errors = [
        pyfltr.command.error_parser.ErrorLocation(
            file="a.py",
            line=1,
            col=None,
            command="ruff-check",
            message="m1",
            rule="F401",
            rule_url="https://docs.astral.sh/ruff/rules/F401/",
        ),
        pyfltr.command.error_parser.ErrorLocation(
            file="b.py",
            line=2,
            col=None,
            command="ruff-check",
            message="m2",
            rule="F401",
            rule_url="https://docs.astral.sh/ruff/rules/F401/",
        ),
        pyfltr.command.error_parser.ErrorLocation(
            file="a.py",
            line=3,
            col=None,
            command="ruff-check",
            message="m3",
            rule="E501",
        ),
    ]
    _, hint_urls, _ = pyfltr.output.jsonl.aggregate_diagnostics(errors)
    assert hint_urls == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_dump_roundtrip() -> None:
    """_dump()のJSON出力がパース可能であることのテスト。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="`os` imported but unused",
        rule="F401",
        severity="error",
        fix="safe",
    )
    records, _, _ = pyfltr.output.jsonl.aggregate_diagnostics([error])
    line = pyfltr.output.jsonl._dump(records[0])
    parsed = json.loads(line)
    assert parsed["kind"] == "diagnostic"
    assert parsed["command"] == "ruff-check"
    assert parsed["messages"][0]["rule"] == "F401"


def test_build_warning_record() -> None:
    """warning dictがkind/source/msgを持つレコードに変換される。"""
    record = pyfltr.output.jsonl._build_warning_record({"source": "config", "message": "foo"})
    assert record == {"kind": "warning", "source": "config", "msg": "foo"}


def test_build_warning_record_with_hint() -> None:
    """hintがあればwarningレコードにhintキーが含まれる。"""
    record = pyfltr.output.jsonl._build_warning_record(
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
    result = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_command_record(
        result,
        diagnostics=1,
        hint_urls={"F401": "https://docs.astral.sh/ruff/rules/F401/"},
    )
    assert record["hint_urls"] == {"F401": "https://docs.astral.sh/ruff/rules/F401/"}


def test_build_command_record_omits_hint_urls_when_empty() -> None:
    """hint_urlsがNone / 空の場合は`hint_urls`キー自体を出さない。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record_none = pyfltr.output.jsonl._build_command_record(result, diagnostics=0, hint_urls=None)
    record_empty = pyfltr.output.jsonl._build_command_record(result, diagnostics=0, hint_urls={})
    assert "hint_urls" not in record_none
    assert "hint_urls" not in record_empty


def test_build_command_record_retry_command_included() -> None:
    """retry_commandが設定されていればtoolレコードに含まれる（失敗時のみpopulateされる前提）。"""
    result = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert record["retry_command"] == "pyfltr run --commands ruff-check -- src/foo.py"


def test_build_command_record_retry_command_omitted() -> None:
    """retry_commandがNoneの場合、toolレコードから省略される。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert "retry_command" not in record


def test_build_command_record_includes_runner_info_when_set() -> None:
    """`effective_runner` / `runner_source` が設定されていればtoolレコードに出力される。

    ユーザーがuv経路を選んだうえで`uv.lock`があるrunの典型値（uv / default）を確認する。
    出力位置は`status`の直後で`files`より前（CLAUDE.md「ツール解決の優先順位」節の追跡用途）。
    """
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["uv", "run", "--frozen", "mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
        effective_runner="uv",
        runner_source="default",
    )
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert record["effective_runner"] == "uv"
    assert record["runner_source"] == "default"
    keys = list(record.keys())
    assert keys.index("status") < keys.index("effective_runner") < keys.index("runner_source")
    assert keys.index("runner_source") < keys.index("files")


def test_build_command_record_runner_info_direct_fallback() -> None:
    """uv経路のdirectフォールバック時は`effective_runner="direct"`が出力される。

    `{command}-runner = "python-runner"`既定経由でグローバル`python-runner = "uv"`既定値に解決される場合でも、
    `uv.lock`欠如時はdirectへフォールバックする（CLAUDE.md「ツール解決の優先順位」節の経路）。
    """
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["/usr/bin/mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
        effective_runner="direct",
        runner_source="default",
    )
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert record["effective_runner"] == "direct"
    assert record["runner_source"] == "default"


def test_build_command_record_omits_runner_info_when_none() -> None:
    """`effective_runner` / `runner_source` がNoneの場合はキーごと省略する。

    `resolution_failed` 経路や対象0件で `build_commandline` を呼ばない経路では
    runner情報が確定しないためNoneのまま出力される（既存の他フィールドと同じ慣習）。
    """
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert "effective_runner" not in record
    assert "runner_source" not in record


def test_build_command_record_runner_info_path_override() -> None:
    """`{command}-path`明示指定時は`runner_source="path-override"`になる。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["/custom/mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
        effective_runner="direct",
        runner_source="path-override",
    )
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert record["effective_runner"] == "direct"
    assert record["runner_source"] == "path-override"


def test_build_command_lines_truncates_diagnostics_when_archived() -> None:
    """jsonl-diagnostic-limit超過時、先頭N件の個別指摘に切り詰めてから集約する。"""
    errors = [
        pyfltr.command.error_parser.ErrorLocation(file="src/foo.py", line=i, col=None, command="mypy", message=f"err{i}")
        for i in range(10)
    ]
    result = pyfltr.command.core_.CommandResult(
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
        pyfltr.command.error_parser.ErrorLocation(file="src/foo.py", line=i, col=None, command="mypy", message=f"err{i}")
        for i in range(10)
    ]
    result = pyfltr.command.core_.CommandResult(
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


def test_build_command_record_cached_includes_cached_from() -> None:
    """cached=Trueのときcached/cached_fromとcached_elapsedがtoolレコードに含まれる。"""
    result = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert record["cached"] is True
    assert record["cached_from"] == "01ABCDEFGH"
    # cached=Trueのときelapsedはださずcached_elapsedだけを出す
    # （LLMが「今回の実行時間」と誤解するのを避ける）。
    assert "elapsed" not in record
    assert record["cached_elapsed"] == 1.23


def test_build_command_record_cached_omitted_when_false() -> None:
    """cached=Falseの場合はcached/cached_from/cached_elapsedが省略されelapsedが出る。"""
    result = pyfltr.command.core_.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=0,
        has_error=False,
        files=3,
        output="",
        elapsed=0.5,
    )
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
    assert "cached" not in record
    assert "cached_elapsed" not in record
    assert record["elapsed"] == 0.5
    assert "cached_from" not in record


def test_build_command_record_cached_without_cached_from() -> None:
    """cached_fromが未設定でもcached=Trueならcached_elapsedは出る。"""
    result = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0)
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
    result = pyfltr.command.core_.CommandResult(
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
    config = pyfltr.config.config.create_default_config()
    record = pyfltr.output.jsonl._build_command_record(result, diagnostics=0, config=config)
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


def test_build_header_record_emits_commands_and_no_schema_hints() -> None:
    """headerレコードにcommands配列が出力され、schema_hintsは出力されない。"""
    record = pyfltr.output.jsonl._build_header_record(commands=["ruff-check", "mypy", "textlint"], files=3, run_id="01TESTULID")
    assert record["run_id"] == "01TESTULID"
    assert record["commands"] == ["ruff-check", "mypy", "textlint"]
    assert "commands_count" not in record
    # schema_hintsは廃止済み
    assert "schema_hints" not in record


def test_build_header_record_size_is_small(monkeypatch: pytest.MonkeyPatch) -> None:
    """headerレコード（実行対象15件想定）は500文字以下に収まる（runner情報追加後）。

    `uv_lock_present` / `uv_available` / `uvx_available` の取得関数を固定値へ差し替え、
    サイズ評価を実行環境（`uv.lock`の有無や`uv` / `uvx`バイナリの導入状況）に依存させない。
    """
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uv_available", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uvx_available", lambda: True)
    commands = [f"tool-{i}" for i in range(15)]
    record = pyfltr.output.jsonl._build_header_record(commands=commands, files=10, run_id="01TESTULID")
    serialized = pyfltr.output.jsonl._dump(record)
    assert len(serialized) <= 500, f"header size {len(serialized)} exceeded 500 chars"


def test_build_header_record_includes_uv_lock_and_uv_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uv_lock_present` / `uv_available` / `uvx_available` がプロセス共通の真偽値として常時出力される。

    Python系コマンドが実行集合に含まれるか否かに関わらず、`mise_active_tools` のような
    条件付き付与ではなく常時出力する設計（runner経路の追跡情報のため）。
    """
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uv_available", lambda: True)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uvx_available", lambda: True)
    record_python = pyfltr.output.jsonl._build_header_record(commands=["mypy"], files=3)
    assert record_python["uv_lock_present"] is True
    assert record_python["uv_available"] is True
    assert record_python["uvx_available"] is True
    # Python系コマンドを含まないrunでも常時出力される。
    record_non_python = pyfltr.output.jsonl._build_header_record(commands=["shellcheck"], files=3)
    assert record_non_python["uv_lock_present"] is True
    assert record_non_python["uv_available"] is True
    assert record_non_python["uvx_available"] is True


def test_build_header_record_uv_fields_reflect_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uv_lock_present` / `uv_available` / `uvx_available` の値が`pyfltr.command.runner`の判定関数に追従する。"""
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda: False)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uv_available", lambda: False)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uvx_available", lambda: False)
    record = pyfltr.output.jsonl._build_header_record(commands=["mypy"], files=3)
    assert record["uv_lock_present"] is False
    assert record["uv_available"] is False
    assert record["uvx_available"] is False


def test_build_header_record_omits_mise_active_tools_when_no_mise_command() -> None:
    """mise経路ツールを含まないrunのheaderには `mise_active_tools` を出さない。"""
    record = pyfltr.output.jsonl._build_header_record(commands=["mypy", "ruff-check"], files=3)
    assert "mise_active_tools" not in record


def test_build_header_record_includes_mise_active_tools_when_passed() -> None:
    """`mise_active_tools` が渡された場合はheaderへ露出する。"""
    record = pyfltr.output.jsonl._build_header_record(
        commands=["cargo-fmt"],
        files=3,
        mise_active_tools={"status": "ok", "active_keys": ["rust"]},
    )
    assert record["mise_active_tools"]["status"] == "ok"
    assert record["mise_active_tools"]["active_keys"] == ["rust"]


def test_collect_mise_active_tools_for_header_skips_when_no_mise_command() -> None:
    """対象commandsにmise登録ツールが無いrunでは `None` を返してheader露出を抑制する。"""
    config = pyfltr.config.config.create_default_config()
    info = pyfltr.output.jsonl.collect_mise_active_tools_for_header(["mypy", "ruff-check"], config)
    assert info is None


def test_collect_mise_active_tools_for_header_includes_when_mise_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """mise登録コマンドが含まれる場合は取得状況dictを返す。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(status="ok", tools={"rust": []}),
    )
    config = pyfltr.config.config.create_default_config()
    info = pyfltr.output.jsonl.collect_mise_active_tools_for_header(["cargo-fmt"], config)
    assert info == {"status": "ok", "active_keys": ["rust"]}


def test_collect_mise_active_tools_for_header_propagates_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """取得失敗時はstatusとdetailをそのまま伝える（active_keysはok時のみ）。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="untrusted-no-side-effects", detail="config not trusted"
        ),
    )
    config = pyfltr.config.config.create_default_config()
    info = pyfltr.output.jsonl.collect_mise_active_tools_for_header(["cargo-fmt"], config)
    assert info == {"status": "untrusted-no-side-effects", "detail": "config not trusted"}


def test_build_summary_record_emits_guidance_on_failure() -> None:
    """failed > 0のときsummary.guidanceが英語で付与され、launcher_prefixとrun_idが埋め込まれる。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_summary_record(
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
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_summary_record([result], exit_code=1)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    joined = " ".join(guidance)
    assert "pyfltr show-run <run_id>" in joined
    assert "pyfltr run-for-agent --only-failed" in joined


def test_build_summary_record_counts_resolution_failed() -> None:
    """resolution_failedはfailedと区別してcommands_summary.needs_action配下に集計され、guidanceも付与される。"""
    result = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_summary_record([result], exit_code=1)
    assert record["commands_summary"]["needs_action"]["failed"] == 0
    assert record["commands_summary"]["needs_action"]["resolution_failed"] == 1
    assert "guidance" in record


def test_build_summary_record_groups_statuses_into_no_issues_and_needs_action() -> None:
    """5種別のステータスがcommands_summary.no_issues / needs_actionの2グループへ正しく振り分けられる。"""
    succeeded = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    formatted = pyfltr.command.core_.CommandResult(
        command="ruff-format",
        command_type="formatter",
        commandline=["ruff", "format"],
        returncode=1,
        has_error=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    skipped = pyfltr.command.core_.CommandResult(
        command="pylint",
        command_type="linter",
        commandline=["pylint"],
        returncode=None,
        has_error=False,
        files=0,
        output="",
        elapsed=0.0,
    )
    failed = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff", "check"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.0,
    )
    resolution_failed = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_summary_record(
        [succeeded, formatted, skipped, failed, resolution_failed],
        exit_code=1,
    )
    commands_summary = record["commands_summary"]
    assert commands_summary["no_issues"] == {"succeeded": 1, "formatted": 1, "skipped": 1}
    # resolution_failedが1件以上のときは出力される
    assert commands_summary["needs_action"]["failed"] == 1
    assert commands_summary["needs_action"]["resolution_failed"] == 1
    # 旧フラットキー・直下のグループキーは廃止されているため、トップレベルから消えていることも確認する。
    assert "no_issues" not in record
    assert "needs_action" not in record
    assert "succeeded" not in record
    assert "formatted" not in record
    assert "failed" not in record
    assert "resolution_failed" not in record
    assert "skipped" not in record


def test_build_summary_record_omits_resolution_failed_when_zero() -> None:
    """`resolution_failed`が0件のときキー自体を省略する。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.0,
    )
    record = pyfltr.output.jsonl._build_summary_record([result], exit_code=1)
    needs_action = record["commands_summary"]["needs_action"]
    assert needs_action["failed"] == 1
    assert "resolution_failed" not in needs_action


def test_build_summary_record_failed_always_present() -> None:
    """`failed`は0件でも常時出力される。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.0,
    )
    record = pyfltr.output.jsonl._build_summary_record([result], exit_code=0)
    needs_action = record["commands_summary"]["needs_action"]
    assert needs_action["failed"] == 0
    assert "resolution_failed" not in needs_action


def test_build_summary_record_no_guidance_on_success() -> None:
    """failed == 0かつapplied_fixesも空のときはsummary.guidanceが省略される。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_summary_record([result], exit_code=0)
    assert "guidance" not in record


def test_build_summary_record_guidance_emits_formatter_notice_only() -> None:
    """failed/resolution_failed=0でもapplied_fixesが非空ならguidanceにformatter書き換え注記1項目だけ出る。"""
    result = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_summary_record([result], exit_code=0)
    guidance = record.get("guidance")
    assert isinstance(guidance, list)
    assert len(guidance) == 1
    assert "formatter/fix-stage rewrote files" in guidance[0]
    assert "re-running is not required" in guidance[0]


def test_build_summary_record_guidance_combines_failure_and_formatter_notice() -> None:
    """failed>0かつapplied_fixes非空のときは失敗時の4項目に続けてformatter書き換え注記が並ぶ。"""
    failed = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    formatted = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_summary_record(
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
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_summary_record(
        [result],
        exit_code=0,
        fully_excluded_files=["docs/ignored.md", "src/also.py"],
    )
    assert record["fully_excluded_files"] == ["docs/ignored.md", "src/also.py"]


def test_build_summary_record_omits_fully_excluded_files_when_empty() -> None:
    """空リスト・Noneの場合はキー自体を出力しない。"""
    result = pyfltr.command.core_.CommandResult(
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
        record = pyfltr.output.jsonl._build_summary_record([result], exit_code=0, fully_excluded_files=value)
        assert "fully_excluded_files" not in record


def test_build_summary_record_includes_missing_targets() -> None:
    """missing_targets指定時はsummaryレコードに出力され、fully_excluded_filesと併存する。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=0,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_summary_record(
        [result],
        exit_code=1,
        missing_targets=["does_not_exist.py", "also_missing.md"],
        fully_excluded_files=["docs/excluded.md"],
    )
    assert record["missing_targets"] == ["does_not_exist.py", "also_missing.md"]
    assert record["fully_excluded_files"] == ["docs/excluded.md"]


def test_build_summary_record_omits_missing_targets_when_empty() -> None:
    """空リスト・Noneの場合はキー自体を出力しない。"""
    result = pyfltr.command.core_.CommandResult(
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
        record = pyfltr.output.jsonl._build_summary_record([result], exit_code=0, missing_targets=value)
        assert "missing_targets" not in record


def test_build_message_dict_includes_end_line_and_end_col() -> None:
    """end_line / end_colが設定されていればmessages[]に出力される。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="a.md",
        line=17,
        col=1,
        command="textlint",
        message="Long sentence (L17:1〜23)",
        rule="ja-technical-writing/sentence-length",
        end_line=17,
        end_col=23,
    )
    record = pyfltr.output.jsonl._build_message_dict(error)
    assert record["end_line"] == 17
    assert record["end_col"] == 23
    # フィールド順はline → col → end_line → end_col → ruleの順
    keys = list(record.keys())
    assert keys.index("col") < keys.index("end_line") < keys.index("end_col") < keys.index("rule")


def test_build_message_dict_omits_end_line_and_end_col_when_none() -> None:
    """end_line / end_colがNoneの場合はキーごと省略する。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="a.py",
        line=10,
        col=5,
        command="mypy",
        message="x",
    )
    record = pyfltr.output.jsonl._build_message_dict(error)
    assert "end_line" not in record
    assert "end_col" not in record


def test_build_message_dict_omits_hint() -> None:
    """hintはmessages[]には出力されない（command.hintsへ集約するため）。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="a.md",
        line=1,
        col=1,
        command="textlint",
        message="文が長すぎます",
        rule="ja-technical-writing/sentence-length",
        hint="Split with periods to shorten.",
    )
    record = pyfltr.output.jsonl._build_message_dict(error)
    assert "hint" not in record


def test_build_command_record_includes_hints_from_errors() -> None:
    """hint付きエラーを与えると`command.hints`にruleごとに1回だけヒント短文が入る。"""
    errors = [
        pyfltr.command.error_parser.ErrorLocation(
            file="a.md",
            line=1,
            col=1,
            command="textlint",
            message="長い文です",
            rule="ja-technical-writing/sentence-length",
            hint=pyfltr.command.error_parser._TEXTLINT_RULE_HINTS["ja-technical-writing/sentence-length"],
        ),
        pyfltr.command.error_parser.ErrorLocation(
            file="a.md",
            line=5,
            col=1,
            command="textlint",
            message="また長い文です",
            rule="ja-technical-writing/sentence-length",
            hint=pyfltr.command.error_parser._TEXTLINT_RULE_HINTS["ja-technical-writing/sentence-length"],
        ),
    ]
    _, _, hints = pyfltr.output.jsonl.aggregate_diagnostics(errors)
    assert hints == {
        "ja-technical-writing/sentence-length": pyfltr.command.error_parser._TEXTLINT_RULE_HINTS[
            "ja-technical-writing/sentence-length"
        ]
    }


def test_build_command_record_hints_key_present_when_hints_given() -> None:
    """`hints`引数が非空なら`command.hints`キーとして埋め込まれる。"""
    result = pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["ruff"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_command_record(
        result,
        diagnostics=1,
        hints={"F401": "Remove unused import."},
    )
    assert record["hints"] == {"F401": "Remove unused import."}


def test_build_command_record_hints_key_omitted_when_empty() -> None:
    """`hints`がNone / 空の場合、textlint以外では`hints`キー自体を出さない。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record_none = pyfltr.output.jsonl._build_command_record(result, diagnostics=0, hints=None)
    record_empty = pyfltr.output.jsonl._build_command_record(result, diagnostics=0, hints={})
    assert "hints" not in record_none
    assert "hints" not in record_empty


def test_build_command_record_textlint_col_hint_only_when_diagnostics() -> None:
    """textlintの`messages[].col`hintは指摘ある時のみ付与され、col/end_colを1個に統合する。

    hint方針（CLAUDE.md「JSONL出力の`command.hints`は対応する指摘やステータスが
    実際に該当するときのみ付与する」）に従い、指摘0件ではhintsキー自体が省略される。
    類似文言の重複を避けるため代表キー`messages[].col`の単一hintで両フィールドを説明する。
    """
    result = pyfltr.command.core_.CommandResult(
        command="textlint",
        command_type="linter",
        commandline=["textlint"],
        returncode=1,
        has_error=True,
        files=1,
        output="",
        elapsed=0.1,
    )
    # 指摘0件: hintsキー自体を出さない
    record_no_diag = pyfltr.output.jsonl._build_command_record(result, diagnostics=0, hints=None)
    assert "hints" not in record_no_diag

    # 指摘1件以上: col仕様注記が入り、rule hintとも併存する
    record_with_hints = pyfltr.output.jsonl._build_command_record(
        result,
        diagnostics=1,
        hints={
            "ja-technical-writing/sentence-length": pyfltr.command.error_parser._TEXTLINT_RULE_HINTS[
                "ja-technical-writing/sentence-length"
            ]
        },
    )
    assert "messages[].col" in record_with_hints["hints"]
    assert "messages[].end_col" not in record_with_hints["hints"]
    col_hint = record_with_hints["hints"]["messages[].col"]
    assert "col" in col_hint and "end_col" in col_hint
    assert "ja-technical-writing/sentence-length" in record_with_hints["hints"]


def test_build_summary_record_includes_applied_fixes() -> None:
    """fixed_filesを持つ結果が複数ある場合、summary.applied_fixesにユニオンしてソートして出力される。

    構築意図: `returncode=1, has_error=False, command_type="formatter"`の組み合わせで
    `status == "formatted"`となるケースを再現する。
    2件の結果で`fixed_files`が重複を含む場合に、ユニオン＆ソートされた一覧が得られることを確認する。
    """
    result_a = pyfltr.command.core_.CommandResult(
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
    result_b = pyfltr.command.core_.CommandResult(
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
    record = pyfltr.output.jsonl._build_summary_record([result_a, result_b], exit_code=0)
    assert record["applied_fixes"] == ["src/a.py", "src/b.py", "src/c.py"]


def test_build_summary_record_omits_applied_fixes_when_empty() -> None:
    """fixed_filesが空のときsummary.applied_fixesは出力されない。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    record = pyfltr.output.jsonl._build_summary_record([result], exit_code=0)
    assert "applied_fixes" not in record
