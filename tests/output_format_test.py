# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=protected-access

import json
import pathlib
import subprocess

import pytest

import pyfltr.archive
import pyfltr.cli
import pyfltr.config
import pyfltr.llm_output
import pyfltr.main
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


@pytest.fixture(name="default_config")
def _default_config() -> pyfltr.config.Config:
    return pyfltr.config.create_default_config()


# ---------------------------------------------------------------------------
# build_linesのユニットテスト
# ---------------------------------------------------------------------------


def test_build_lines_supported_tool_diagnostics(default_config):
    """error_parser対応ツールの診断が（command, file）単位で集約されたdiagnosticレコードとして出ること。"""
    errors = [
        _make_error("mypy", "src/a.py", 10, "bad type", col=4),
        _make_error("mypy", "src/a.py", 20, "missing return"),
    ]
    result = _make_result("mypy", returncode=1, errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1, commands=["mypy"], files=5)
    parsed = [json.loads(line) for line in lines]

    # 同一（mypy, src/a.py）に集約されるためdiagnosticは1行
    assert [r["kind"] for r in parsed] == ["header", "diagnostic", "command", "summary"]
    assert parsed[1] == {
        "kind": "diagnostic",
        "command": "mypy",
        "file": "src/a.py",
        "messages": [
            {"line": 10, "col": 4, "msg": "bad type"},
            {"line": 20, "msg": "missing return"},
        ],
    }
    assert parsed[2]["diagnostics"] == 2
    assert parsed[2]["status"] == "failed"
    assert parsed[3]["diagnostics"] == 2
    assert parsed[3]["commands_summary"]["needs_action"]["failed"] == 1


def test_build_lines_warnings_prepended(default_config):
    """warnings引数の内容がdiagnosticより前にkind="warning"で出力されること。"""
    result = _make_result("ruff-format", returncode=0, command_type="formatter")
    warnings = [
        {"source": "config", "message": "pre-commit 設定ファイル不在"},
        {"source": "git", "message": "git が見つからない"},
    ]
    lines = pyfltr.llm_output.build_lines(
        [result], default_config, exit_code=0, commands=["ruff-format"], files=1, warnings=warnings
    )
    parsed = [json.loads(line) for line in lines]

    assert parsed[0]["kind"] == "header"
    assert [r["kind"] for r in parsed[1:3]] == ["warning", "warning"]
    assert parsed[1] == {"kind": "warning", "source": "config", "msg": "pre-commit 設定ファイル不在"}
    assert parsed[2] == {"kind": "warning", "source": "git", "msg": "git が見つからない"}
    # warningsの後にtoolレコード、最後にsummaryが並ぶ
    assert [r["kind"] for r in parsed[3:]] == ["command", "summary"]


def test_build_lines_no_warnings_when_omitted(default_config):
    """warnings引数を省略するとwarningレコードは出ない。"""
    result = _make_result("ruff-format", returncode=0, command_type="formatter")
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0)
    parsed = [json.loads(line) for line in lines]
    assert all(r["kind"] != "warning" for r in parsed)


def test_build_lines_unsupported_tool_only(default_config):
    """error_parser非対応ツール（ruff-format）はtoolレコードのみ（header省略時）。"""
    result = _make_result("ruff-format", returncode=1, command_type="formatter", has_error=False)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    parsed = [json.loads(line) for line in lines]

    assert [r["kind"] for r in parsed] == ["command", "summary"]
    assert parsed[0]["command"] == "ruff-format"
    assert parsed[0]["status"] == "formatted"
    assert parsed[0]["diagnostics"] == 0
    assert "message" not in parsed[0]
    assert parsed[1]["commands_summary"]["no_issues"]["formatted"] == 1


def test_build_lines_mixed_order(default_config):
    """ツール単位でdiagnostic+toolがグルーピングされ、config.command_names順に並ぶこと。"""
    mypy_result = _make_result(
        "mypy",
        returncode=1,
        errors=[
            _make_error("mypy", "src/b.py", 5, "later"),
            _make_error("mypy", "src/a.py", 30, "earlier in a"),
        ],
    )
    pylint_result = _make_result(
        "pylint",
        returncode=1,
        errors=[_make_error("pylint", "src/a.py", 10, "C0114: missing docstring")],
    )
    ruff_format_result = _make_result("ruff-format", returncode=0, command_type="formatter")

    # config.command_names順ではruff-format → mypy → pylint
    lines = pyfltr.llm_output.build_lines(
        [mypy_result, pylint_result, ruff_format_result],
        default_config,
        exit_code=1,
        commands=["ruff-format", "mypy", "pylint"],
        files=10,
    )
    parsed = [json.loads(line) for line in lines]

    # header → ツール順でグルーピング: ruff-format（tool）→ mypy（a.pyとb.pyの2 diagnostic + tool）
    # → pylint（diagnostic + tool）→ summary。（command, file）単位で集約される
    assert [r["kind"] for r in parsed] == [
        "header",
        "command",  # ruff-format
        "diagnostic",  # mypy / src/a.py
        "diagnostic",  # mypy / src/b.py
        "command",  # mypy
        "diagnostic",  # pylint / src/a.py
        "command",  # pylint
        "summary",
    ]

    # mypy内のdiagnosticはファイル順
    mypy_diagnostics = [r for r in parsed if r["kind"] == "diagnostic" and r["command"] == "mypy"]
    assert [(r["file"], r["messages"][0]["line"]) for r in mypy_diagnostics] == [
        ("src/a.py", 30),
        ("src/b.py", 5),
    ]

    tool_records = [r for r in parsed if r["kind"] == "command"]
    assert [r["command"] for r in tool_records] == ["ruff-format", "mypy", "pylint"]


def test_build_lines_ensure_ascii_false(default_config):
    """日本語メッセージが生のまま出ること（ensure_ascii=False）。"""
    errors = [_make_error("mypy", "src/a.py", 1, "型が合いません")]
    result = _make_result("mypy", returncode=1, errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    assert "型が合いません" in lines[0]
    assert "\\u" not in lines[0]


def test_build_lines_skipped_status(default_config):
    """returncode=None（skipped）はrcキーを省略しdiagnostics=0のtoolレコードを出す。"""
    result = _make_result("mypy", returncode=None, has_error=False)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0)
    parsed = [json.loads(line) for line in lines]

    tool_record = parsed[0]
    assert tool_record["kind"] == "command"
    assert tool_record["status"] == "skipped"
    assert "rc" not in tool_record


# ---------------------------------------------------------------------------
# toolレコードのmessageフィールド
# ---------------------------------------------------------------------------


def test_command_record_message_on_failure_without_diagnostics(default_config):
    """status=failedかつdiagnostics=0のとき、output末尾がmessageに入ること。"""
    output = "line1\nline2\nError: command not found\n"
    result = _make_result("shellcheck", returncode=127, output=output)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    assert tool_record["status"] == "failed"
    assert "message" in tool_record
    assert "Error: command not found" in tool_record["message"]


def test_command_record_message_truncates_long_output(default_config):
    """長いoutputはハイブリッド方式（先頭 + マーカー + 末尾30行）でトリムされること。"""
    many_lines = "\n".join(f"line{i}" for i in range(100))
    result = _make_result("shellcheck", returncode=1, output=many_lines)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    msg = tool_record["message"]
    assert "... (truncated)" in msg
    # 先頭側は冒頭行を保持する。
    assert msg.startswith("line0")
    # 末尾側は末尾30行（line70..line99）のみを残す。
    assert "line99" in msg
    assert "line70" in msg
    assert "line69" not in msg
    assert len(msg) <= 2000 + len("... (truncated)") + 4


def test_command_record_no_message_when_diagnostics_present(default_config):
    """failedでもdiagnostics > 0のときはmessageを出さない。"""
    errors = [_make_error("mypy", "src/a.py", 1, "bad")]
    result = _make_result("mypy", returncode=1, output="verbose mypy output", errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = next(json.loads(line) for line in lines if json.loads(line)["kind"] == "command")
    assert "message" not in tool_record


def test_command_record_no_message_on_success(default_config):
    """status=succeeded/formattedではmessageを出さない。"""
    ok = _make_result("mypy", returncode=0, output="all ok")
    fmt = _make_result("ruff-format", returncode=1, command_type="formatter", output="reformatted", has_error=False)
    lines = pyfltr.llm_output.build_lines([ok, fmt], default_config, exit_code=0)
    for line in lines:
        record = json.loads(line)
        if record["kind"] == "command":
            assert "message" not in record


def test_command_record_no_message_when_output_empty(default_config):
    """failedでもoutputが空ならmessageを出さない（キーごと省略）。"""
    result = _make_result("shellcheck", returncode=1, output="")
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    assert "message" not in tool_record


# ---------------------------------------------------------------------------
# structured_logger経由の書き出し
# ---------------------------------------------------------------------------


def test_calculate_returncode_matches_summary_exit(default_config):
    """summary.exitとcalculate_returncodeの戻り値が一致すること。"""
    results = [
        _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "bad")]),
        _make_result("ruff-format", returncode=0, command_type="formatter"),
    ]
    exit_code = pyfltr.main.calculate_returncode(results, exit_zero_even_if_formatted=False)
    lines = pyfltr.llm_output.build_lines(
        results, default_config, exit_code=exit_code, commands=["mypy", "ruff-format"], files=3
    )
    summary = json.loads(lines[-1])
    assert summary["exit"] == exit_code == 1


# ---------------------------------------------------------------------------
# CLI統合テスト（pyfltr.main.run）
# ---------------------------------------------------------------------------


def test_run_cli_jsonl_stdout_suppresses_text(mocker, capsys):
    """jsonl + stdoutモードではstdoutはJSONLのみでtextはstderr（WARN+）扱いになる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    captured = capsys.readouterr()
    # stdoutはJSONLのみ。text整形の区切り線が混入しないこと。
    assert "----- pyfltr" not in captured.out
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONLが1行も出ていない"
    first = json.loads(lines[0])
    assert first["kind"] == "header"
    # 実行対象のみのcommands配列として出す。commands_countは廃止済み。
    assert first["commands"] == ["mypy"]
    assert "commands_count" not in first
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"
    assert last["exit"] == 0
    # stderrにはINFO進捗・summaryが出ない（jsonl stdoutはWARN以上）
    assert "----- pyfltr" not in captured.err
    assert "----- summary" not in captured.err


def test_run_cli_output_file_keeps_text_stdout(mocker, capsys, tmp_path):
    """--output-file指定時はstdoutには従来text、ファイルにはJSONL。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    destination = tmp_path / "out.jsonl"
    returncode = pyfltr.main.run(
        [
            "ci",
            "--output-format=jsonl",
            f"--output-file={destination}",
            "--commands=mypy",
            str(pathlib.Path(__file__).parent.parent),
        ]
    )
    assert returncode == 0
    captured = capsys.readouterr()
    # 従来のtext出力がstdoutに出る
    assert "summary" in captured.out
    # ファイルにはJSONL
    lines = destination.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["kind"] == "summary"


def test_run_cli_jsonl_ignores_ui(mocker, capsys):
    """jsonl + stdoutモードでは--uiがsilently無効化される。stdoutはJSONLのみ。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(
        ["ci", "--output-format=jsonl", "--ui", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONLが1行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"


def test_run_cli_env_var_jsonl(mocker, capsys, monkeypatch):
    """PYFLTR_OUTPUT_FORMAT=jsonlで--output-format未指定でもJSONL出力になる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "jsonl")

    returncode = pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    captured = capsys.readouterr()
    assert "----- pyfltr" not in captured.out
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONLが1行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"


def test_run_cli_env_var_overridden_by_cli(mocker, capsys, monkeypatch):
    """PYFLTR_OUTPUT_FORMATよりCLI --output-format=textが優先される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "jsonl")

    pyfltr.main.run(["ci", "--output-format=text", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    # CLIでtextを明示しているのでtext整形出力がstdoutに出るべき
    assert "summary" in captured.out


def test_run_cli_env_var_invalid(monkeypatch):
    """PYFLTR_OUTPUT_FORMATに不正値が入っている場合はSystemExitで終了する。"""
    # 実行系サブコマンドの解決ロジックを直接呼び出して環境変数バリデーションを確認する。
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "yaml")
    parser = pyfltr.main.build_parser()
    args = parser.parse_args(["ci"])
    with pytest.raises(SystemExit):
        pyfltr.main._resolve_output_format(parser, args)


def test_run_cli_ai_agent_jsonl(mocker, capsys, monkeypatch):
    """AI_AGENT が設定されていれば、--output-format 未指定でも JSONL 出力になる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("AI_AGENT", "1")

    returncode = pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    captured = capsys.readouterr()
    assert "----- pyfltr" not in captured.out
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONLが1行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"


def test_run_cli_ai_agent_overridden_by_env_var(mocker, capsys, monkeypatch):
    """PYFLTR_OUTPUT_FORMAT は AI_AGENT より優先される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("AI_AGENT", "1")
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "text")

    pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    # PYFLTR_OUTPUT_FORMAT=text が優先され、stdoutにtext整形（区切り線）が出る。
    assert "----- pyfltr" in captured.out
    assert "----- summary" in captured.out


def test_run_cli_ai_agent_overridden_by_cli(mocker, capsys, monkeypatch):
    """CLI --output-format は AI_AGENT より優先される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("AI_AGENT", "1")

    pyfltr.main.run(["ci", "--output-format=text", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "----- pyfltr" in captured.out
    assert "----- summary" in captured.out


def test_run_cli_ai_agent_empty_string_unset(mocker, capsys, monkeypatch):
    """AI_AGENT が空文字列の場合は未設定扱い（textに戻る）。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("AI_AGENT", "")

    pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "----- pyfltr" in captured.out
    assert "----- summary" in captured.out


def test_run_cli_ai_agent_zero_value_truthy(mocker, capsys, monkeypatch):
    """AI_AGENT は値の中身を問わず、設定されていれば真扱い（"0"でもJSONL）。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("AI_AGENT", "0")

    pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONLが1行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"


def test_run_for_agent_env_var_text_override(mocker, capsys, monkeypatch):
    """PYFLTR_OUTPUT_FORMAT=text は run-for-agent のサブコマンド既定値より優先される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "text")

    pyfltr.main.run(["run-for-agent", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    # PYFLTR_OUTPUT_FORMAT=text が run-for-agent の jsonl 既定より優先される。
    assert "----- pyfltr" in captured.out
    assert "----- summary" in captured.out


def test_resolve_output_format_returns_resolution():
    """`resolve_output_format`が決定値+由来ラベルのdataclassを返す（CLI明示時は`cli`）。"""
    parser = pyfltr.main.build_parser()
    resolution = pyfltr.cli.resolve_output_format(
        parser,
        "jsonl",
        valid_values=frozenset({"text", "jsonl"}),
        ai_agent_default="jsonl",
    )
    assert resolution.format == "jsonl"
    assert resolution.source == pyfltr.cli.FORMAT_SOURCE_CLI


def test_resolve_output_format_env_pyfltr(monkeypatch):
    """`PYFLTR_OUTPUT_FORMAT`明示時は由来ラベルが`env.PYFLTR_OUTPUT_FORMAT`になる。"""
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "jsonl")
    parser = pyfltr.main.build_parser()
    resolution = pyfltr.cli.resolve_output_format(
        parser,
        None,
        valid_values=frozenset({"text", "jsonl"}),
        ai_agent_default="jsonl",
    )
    assert resolution.format == "jsonl"
    assert resolution.source == pyfltr.cli.FORMAT_SOURCE_ENV_PYFLTR


def test_resolve_output_format_subcommand_default():
    """サブコマンド既定値経路では由来ラベルが`subcommand_default`になる。"""
    parser = pyfltr.main.build_parser()
    resolution = pyfltr.cli.resolve_output_format(
        parser,
        None,
        valid_values=frozenset({"text", "jsonl"}),
        subcommand_default="jsonl",
        ai_agent_default="jsonl",
    )
    assert resolution.format == "jsonl"
    assert resolution.source == pyfltr.cli.FORMAT_SOURCE_SUBCOMMAND_DEFAULT


def test_resolve_output_format_env_ai_agent(monkeypatch):
    """`AI_AGENT`設定時は由来ラベルが`env.AI_AGENT`、形式は`ai_agent_default`の値になる。"""
    monkeypatch.setenv("AI_AGENT", "1")
    parser = pyfltr.main.build_parser()
    resolution = pyfltr.cli.resolve_output_format(
        parser,
        None,
        valid_values=frozenset({"text", "jsonl"}),
        ai_agent_default="jsonl",
    )
    assert resolution.format == "jsonl"
    assert resolution.source == pyfltr.cli.FORMAT_SOURCE_ENV_AI_AGENT


def test_resolve_output_format_fallback():
    """いずれの経路にも該当しない場合は`fallback`扱いで`final_default`を返す。"""
    parser = pyfltr.main.build_parser()
    resolution = pyfltr.cli.resolve_output_format(
        parser,
        None,
        valid_values=frozenset({"text", "jsonl"}),
    )
    assert resolution.format == "text"
    assert resolution.source == pyfltr.cli.FORMAT_SOURCE_FALLBACK


def test_resolve_output_format_ai_agent_default_none_ignores_env(monkeypatch):
    """`ai_agent_default=None`では`AI_AGENT`が立っていてもfallbackへ進む。"""
    monkeypatch.setenv("AI_AGENT", "1")
    parser = pyfltr.main.build_parser()
    resolution = pyfltr.cli.resolve_output_format(
        parser,
        None,
        valid_values=frozenset({"text", "jsonl"}),
    )
    assert resolution.format == "text"
    assert resolution.source == pyfltr.cli.FORMAT_SOURCE_FALLBACK


def test_run_cli_header_format_source_subcommand_default(mocker, capsys):
    """run-for-agentの既定値経路ではheader.format_sourceが`subcommand_default`になる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    pyfltr.main.run(["run-for-agent", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    header = json.loads(lines[0])
    assert header["kind"] == "header"
    assert header["format_source"] == pyfltr.cli.FORMAT_SOURCE_SUBCOMMAND_DEFAULT


def test_run_cli_header_format_source_cli(mocker, capsys):
    """`--output-format=jsonl`明示時はheader.format_sourceが`cli`になる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    pyfltr.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    header = json.loads(lines[0])
    assert header["format_source"] == pyfltr.cli.FORMAT_SOURCE_CLI


def test_run_cli_header_format_source_env_ai_agent(mocker, capsys, monkeypatch):
    """`AI_AGENT`設定時のJSONL既定切替ではheader.format_sourceが`env.AI_AGENT`になる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("AI_AGENT", "1")

    pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    header = json.loads(lines[0])
    assert header["format_source"] == pyfltr.cli.FORMAT_SOURCE_ENV_AI_AGENT


def test_run_cli_header_format_source_env_pyfltr(mocker, capsys, monkeypatch):
    """`PYFLTR_OUTPUT_FORMAT=jsonl`経路ではheader.format_sourceが`env.PYFLTR_OUTPUT_FORMAT`になる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "jsonl")

    pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    header = json.loads(lines[0])
    assert header["format_source"] == pyfltr.cli.FORMAT_SOURCE_ENV_PYFLTR


def test_command_record_formatted_status_hint(default_config):
    """`status="formatted"`のcommandレコードには再実行不要を示すhintが入る。"""
    result = _make_result("ruff-format", returncode=1, command_type="formatter", has_error=False)
    lines = pyfltr.llm_output.build_command_lines(result, default_config)
    parsed = [json.loads(line) for line in lines]
    command_record = parsed[-1]
    assert command_record["status"] == "formatted"
    assert "status.formatted" in command_record["hints"]
    assert "rerun is not required" in command_record["hints"]["status.formatted"]


def test_command_record_non_formatted_no_status_hint(default_config):
    """`status="formatted"`以外のcommandレコードには`status.formatted`ヒントを出さない。"""
    result = _make_result("mypy", returncode=0, output="ok")
    lines = pyfltr.llm_output.build_command_lines(result, default_config)
    parsed = [json.loads(line) for line in lines]
    command_record = parsed[-1]
    assert command_record["status"] == "succeeded"
    assert "hints" not in command_record


def test_get_status_text_formatted_includes_no_rerun_needed():
    """text出力サマリー行のformatted行末尾に`; no rerun needed`が付く。"""
    result = _make_result("ruff-format", returncode=1, command_type="formatter", has_error=False)
    text = result.get_status_text()
    assert text.startswith("formatted (")
    assert text.endswith("; no rerun needed")


def test_get_status_text_succeeded_no_extra_message():
    """succeeded等のformatted以外には`; no rerun needed`を付けない。"""
    result = _make_result("mypy", returncode=0)
    text = result.get_status_text()
    assert "no rerun needed" not in text


def test_run_cli_jsonl_restores_logger_state(mocker, capsys):
    """jsonlモード実行後にtextモードを再実行すると、text出力がstdoutに戻ること。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    # 1回目: jsonlモード（stdoutにJSONL、textはstderrのWARN+）
    pyfltr.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    # 1回目のstdoutは読み捨てる（capsysをリセット）
    capsys.readouterr()

    # 2回目: textモード（従来どおりのログが出るべき）。
    pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "summary" in captured.out


# ---------------------------------------------------------------------------
# build_command_linesのユニットテスト
# ---------------------------------------------------------------------------


def test_build_command_lines_with_diagnostics(default_config):
    """diagnostic行+tool行がツール単位で（command, file）集約されてまとまること。"""
    errors = [
        _make_error("mypy", "src/b.py", 5, "later"),
        _make_error("mypy", "src/a.py", 10, "earlier"),
    ]
    result = _make_result("mypy", returncode=1, errors=errors)
    lines = pyfltr.llm_output.build_command_lines(result, default_config)
    parsed = [json.loads(line) for line in lines]

    assert len(parsed) == 3
    # diagnostic行はツール内でファイル順にソートされる（src/a.py→src/b.py）
    assert parsed[0]["kind"] == "diagnostic"
    assert parsed[0]["file"] == "src/a.py"
    assert parsed[0]["messages"][0]["line"] == 10
    assert parsed[1]["kind"] == "diagnostic"
    assert parsed[1]["file"] == "src/b.py"
    assert parsed[1]["messages"][0]["line"] == 5
    # 最後にtool行
    assert parsed[2]["kind"] == "command"
    assert parsed[2]["diagnostics"] == 2


def test_build_command_lines_no_diagnostics(default_config):
    """diagnosticがないツールはtool行のみ。"""
    result = _make_result("ruff-format", returncode=0, command_type="formatter")
    lines = pyfltr.llm_output.build_command_lines(result, default_config)
    parsed = [json.loads(line) for line in lines]

    assert len(parsed) == 1
    assert parsed[0]["kind"] == "command"
    assert parsed[0]["diagnostics"] == 0


def test_build_command_lines_truncated_archive_sanitizes_command_name(default_config, tmp_path):
    """サニタイズ対象文字を含むcommand名でも`truncated.archive`は実保存キーと一致する。

    `archive.ArchiveStore.write_tool_result`が書き込む保存キーと
    `command.truncated.archive`が参照するパスが同じサニタイズ関数を通ることを検証する。
    カスタムコマンド名にスラッシュや空白が入る潜在シナリオを想定したリグレッション防止。
    """
    command_name = "foo/bar baz"
    sanitized = "foo_bar_baz"
    # diagnostic切り詰めを発生させるためerrorsを複数件用意
    errors = [_make_error(command_name, "src/x.py", i, f"err{i}") for i in range(5)]
    result = _make_result(command_name, returncode=1, errors=errors)

    default_config.values["jsonl-diagnostic-limit"] = 2
    lines = pyfltr.llm_output.build_command_lines(result, default_config)
    tool_record = next(json.loads(line) for line in lines if json.loads(line)["kind"] == "command")
    assert tool_record["truncated"]["archive"] == f"tools/{sanitized}/diagnostics.jsonl"

    # message切り詰めでもサニタイズされたキーになること
    long_output = "\n".join(f"line{i}" for i in range(100))
    result_msg = _make_result(command_name, returncode=1, output=long_output)
    lines = pyfltr.llm_output.build_command_lines(result_msg, default_config)
    tool_record = next(json.loads(line) for line in lines if json.loads(line)["kind"] == "command")
    assert tool_record["truncated"]["archive"] == f"tools/{sanitized}/output.log"

    # archive側が同じ保存キーを使うことを実アーカイブ書き込みで検証
    store = pyfltr.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=[command_name])
    store.write_tool_result(run_id, result)
    assert (tmp_path / "runs" / run_id / "tools" / sanitized / "diagnostics.jsonl").exists()


# ---------------------------------------------------------------------------
# write_jsonl_streamingのユニットテスト
# ---------------------------------------------------------------------------


def _configure_structured_stdout() -> None:
    """テストのためstructured_loggerを現在の`sys.stdout`に向ける。

    `capsys`フィクスチャは`sys.stdout`を差し替えているため、呼び出し時点の
    `sys.stdout`をそのままStreamHandlerに掴ませればcapsysで拾える。
    """
    import sys  # pylint: disable=import-outside-toplevel

    pyfltr.cli.configure_structured_output(sys.stdout)


def test_write_jsonl_streaming(default_config, capsys):
    """ストリーミング書き出しがstdoutに即時出力されること。"""
    _configure_structured_stdout()
    errors = [_make_error("mypy", "src/a.py", 10, "bad type")]
    result = _make_result("mypy", returncode=1, errors=errors)
    pyfltr.llm_output.write_jsonl_streaming(result, default_config)

    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = [json.loads(line) for line in captured.out.splitlines()]
    assert len(parsed) == 2
    assert parsed[0]["kind"] == "diagnostic"
    assert parsed[1]["kind"] == "command"


# ---------------------------------------------------------------------------
# write_jsonl_footerのユニットテスト
# ---------------------------------------------------------------------------


def test_write_jsonl_footer_with_warnings(capsys):
    """warning行+summary行がstdoutに出力されること。"""
    _configure_structured_stdout()
    result = _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "bad")])
    warnings = [{"source": "config", "message": "test warning"}]
    pyfltr.llm_output.write_jsonl_footer(
        [result],
        exit_code=1,
        warnings=warnings,
    )

    captured = capsys.readouterr()
    parsed = [json.loads(line) for line in captured.out.splitlines()]
    assert len(parsed) == 2
    assert parsed[0]["kind"] == "warning"
    assert parsed[0]["msg"] == "test warning"
    assert parsed[1]["kind"] == "summary"
    assert parsed[1]["exit"] == 1


def test_write_jsonl_footer_no_warnings(capsys):
    """warningがない場合はsummary行のみ。"""
    _configure_structured_stdout()
    result = _make_result("mypy", returncode=0)
    pyfltr.llm_output.write_jsonl_footer([result], exit_code=0)

    captured = capsys.readouterr()
    parsed = [json.loads(line) for line in captured.out.splitlines()]
    assert len(parsed) == 1
    assert parsed[0]["kind"] == "summary"
    assert parsed[0]["commands_summary"]["no_issues"]["succeeded"] == 1


# ---------------------------------------------------------------------------
# headerレコードのユニットテスト
# ---------------------------------------------------------------------------


def test_build_header_record_fields():
    """`_build_header_record`が必要なフィールドをすべて含むこと（commandsは実行対象配列）。"""
    record = pyfltr.llm_output._build_header_record(["ruff-format", "mypy"], 42)
    assert record["kind"] == "header"
    assert record["commands"] == ["ruff-format", "mypy"]
    assert "commands_count" not in record
    assert record["files"] == 42
    assert "version" in record
    assert "python" in record
    assert "executable" in record
    assert "platform" in record
    assert "cwd" in record


def test_build_lines_header_first(default_config):
    """commands/filesを指定するとheader行が先頭に出力され、commandsは配列で入ること。"""
    result = _make_result("mypy", returncode=0)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0, commands=["mypy"], files=10)
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["kind"] == "header"
    assert parsed[0]["commands"] == ["mypy"]
    assert "commands_count" not in parsed[0]
    assert parsed[0]["files"] == 10
    assert parsed[-1]["kind"] == "summary"


def test_build_lines_no_header_when_omitted(default_config):
    """commands/filesを省略するとheader行は出力されないこと。"""
    result = _make_result("mypy", returncode=0)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0)
    parsed = [json.loads(line) for line in lines]
    assert all(r["kind"] != "header" for r in parsed)


def test_write_jsonl_header_stdout(capsys):
    """`write_jsonl_header`がstdoutにheader行を書き出し、commandsが配列になること。"""
    _configure_structured_stdout()
    pyfltr.llm_output.write_jsonl_header(commands=["ruff-format", "mypy"], files=5)
    captured = capsys.readouterr()
    parsed = [json.loads(line) for line in captured.out.splitlines()]
    assert len(parsed) == 1
    assert parsed[0]["kind"] == "header"
    assert parsed[0]["commands"] == ["ruff-format", "mypy"]
    assert "commands_count" not in parsed[0]
    assert parsed[0]["files"] == 5


# ---------------------------------------------------------------------------
# --commandsの繰り返し指定（action="append"）+ カンマ区切り併用のテスト
# ---------------------------------------------------------------------------


def _header_commands_for(args: list[str], mocker, capsys) -> list[str]:
    """指定CLI引数でrun-for-agentを走らせheaderのcommands配列を取り出す。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    target = str(pathlib.Path(__file__).parent.parent)
    returncode = pyfltr.main.run(["run-for-agent", *args, target])
    assert returncode == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    header = json.loads(lines[0])
    assert header["kind"] == "header"
    return header["commands"]


def test_commands_option_repeated_and_comma_separated_equivalent(mocker, capsys):
    """--commandsの複数回指定とカンマ区切りが同じcommands配列を生成する。"""
    repeated = _header_commands_for(["--commands=mypy", "--commands=pyright"], mocker, capsys)
    comma = _header_commands_for(["--commands=mypy,pyright"], mocker, capsys)
    assert repeated == comma == ["mypy", "pyright"]


def test_commands_option_mixed_repeated_and_comma(mocker, capsys):
    """--commandsの繰り返しとカンマ区切りを混在指定できる（後勝ちではなくマージ）。"""
    commands = _header_commands_for(["--commands=mypy", "--commands=pyright,ruff-check"], mocker, capsys)
    # 実際の実行順はconfig.command_names定義順に並ぶが、少なくとも3ツールがマージされていること。
    assert set(commands) == {"mypy", "pyright", "ruff-check"}


def test_commands_option_dedup_preserves_first_occurrence(mocker, capsys):
    """重複指定されたコマンドは1回だけ実行対象に含まれる。"""
    commands = _header_commands_for(["--commands=mypy,pyright", "--commands=mypy"], mocker, capsys)
    assert commands.count("mypy") == 1
    assert "pyright" in commands


# ---------------------------------------------------------------------------
# code-quality形式のCLI統合テスト
# ---------------------------------------------------------------------------


_REQUIRED_CQ_FIELDS = {"description", "check_name", "fingerprint", "severity", "location"}


def test_run_cli_code_quality_stdout(mocker, capsys):
    """code-quality + stdoutモードではstdoutはJSON配列、stderrにtext整形が出る。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(
        ["ci", "--output-format=code-quality", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0
    captured = capsys.readouterr()
    # stdoutはJSON配列1件
    payload = json.loads(captured.out)
    assert isinstance(payload, list)
    # code-qualityは診断なしなら空配列だが、ルート型がlistである確認が主眼
    for issue in payload:
        assert issue.keys() >= _REQUIRED_CQ_FIELDS
        assert (issue.keys() | {"location"}) >= _REQUIRED_CQ_FIELDS
        assert {"path", "lines"} <= issue["location"].keys()
        assert "begin" in issue["location"]["lines"]
    # stderrにはtext整形（進捗・summary）が出る
    assert "----- pyfltr" in captured.err
    assert "----- summary" in captured.err


def test_run_cli_code_quality_output_file(mocker, capsys, tmp_path):
    """code-quality + --output-fileではファイルにJSON配列、stdoutにtext整形。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    destination = tmp_path / "gl.json"
    returncode = pyfltr.main.run(
        [
            "ci",
            "--output-format=code-quality",
            f"--output-file={destination}",
            "--commands=mypy",
            str(pathlib.Path(__file__).parent.parent),
        ]
    )
    assert returncode == 0
    captured = capsys.readouterr()
    # stdoutには従来のtext整形出力
    assert "summary" in captured.out
    # ファイルはJSON配列
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert isinstance(payload, list)


def test_run_cli_code_quality_with_diagnostics(mocker, capsys):
    """エラーを検出したツールでCode Quality必須フィールドを満たすissueが出る。"""
    mypy_output = "src/a.py:10: error: bad type  [arg-type]\n"
    proc = subprocess.CompletedProcess(["mypy"], returncode=1, stdout=mypy_output)
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    pyfltr.main.run(["ci", "--output-format=code-quality", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, list)
    assert len(payload) >= 1
    issue = payload[0]
    assert issue.keys() >= _REQUIRED_CQ_FIELDS
    assert issue["check_name"].startswith("mypy")
    assert issue["location"]["lines"]["begin"] >= 1
    assert issue["severity"] in ("info", "minor", "major", "critical", "blocker")
