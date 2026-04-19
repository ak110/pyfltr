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
# build_lines のユニットテスト
# ---------------------------------------------------------------------------


def test_build_lines_supported_tool_diagnostics(default_config):
    """error_parser 対応ツールの診断が (command, file) 単位で集約された diagnostic レコードとして出ること。"""
    errors = [
        _make_error("mypy", "src/a.py", 10, "bad type", col=4),
        _make_error("mypy", "src/a.py", 20, "missing return"),
    ]
    result = _make_result("mypy", returncode=1, errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1, commands=["mypy"], files=5)
    parsed = [json.loads(line) for line in lines]

    # 同一 (mypy, src/a.py) に集約されるため diagnostic は 1 行
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
    assert parsed[3]["failed"] == 1


def test_build_lines_warnings_prepended(default_config):
    """warnings 引数の内容が diagnostic より前に kind="warning" で出力されること。"""
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
    # warnings の後に tool レコード、最後に summary が並ぶ
    assert [r["kind"] for r in parsed[3:]] == ["command", "summary"]


def test_build_lines_no_warnings_when_omitted(default_config):
    """warnings 引数を省略すると warning レコードは出ない。"""
    result = _make_result("ruff-format", returncode=0, command_type="formatter")
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0)
    parsed = [json.loads(line) for line in lines]
    assert all(r["kind"] != "warning" for r in parsed)


def test_build_lines_unsupported_tool_only(default_config):
    """error_parser 非対応ツール (ruff-format) は tool レコードのみ（header省略時）。"""
    result = _make_result("ruff-format", returncode=1, command_type="formatter", has_error=False)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    parsed = [json.loads(line) for line in lines]

    assert [r["kind"] for r in parsed] == ["command", "summary"]
    assert parsed[0]["command"] == "ruff-format"
    assert parsed[0]["status"] == "formatted"
    assert parsed[0]["diagnostics"] == 0
    assert "message" not in parsed[0]
    assert parsed[1]["formatted"] == 1


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

    # config.command_names 順では ruff-format → mypy → pylint
    lines = pyfltr.llm_output.build_lines(
        [mypy_result, pylint_result, ruff_format_result],
        default_config,
        exit_code=1,
        commands=["ruff-format", "mypy", "pylint"],
        files=10,
    )
    parsed = [json.loads(line) for line in lines]

    # header → ツール順でグルーピング: ruff-format(tool) → mypy(a.pyとb.pyの2 diagnostic + tool)
    # → pylint(diagnostic + tool) → summary。(command, file)単位で集約される
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

    # mypy 内の diagnostic はファイル順
    mypy_diagnostics = [r for r in parsed if r["kind"] == "diagnostic" and r["command"] == "mypy"]
    assert [(r["file"], r["messages"][0]["line"]) for r in mypy_diagnostics] == [
        ("src/a.py", 30),
        ("src/b.py", 5),
    ]

    tool_records = [r for r in parsed if r["kind"] == "command"]
    assert [r["command"] for r in tool_records] == ["ruff-format", "mypy", "pylint"]


def test_build_lines_ensure_ascii_false(default_config):
    """日本語メッセージが生のまま出ること (ensure_ascii=False)。"""
    errors = [_make_error("mypy", "src/a.py", 1, "型が合いません")]
    result = _make_result("mypy", returncode=1, errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    assert "型が合いません" in lines[0]
    assert "\\u" not in lines[0]


def test_build_lines_skipped_status(default_config):
    """returncode=None (skipped) は rc キーを省略し diagnostics=0 の tool レコードを出す。"""
    result = _make_result("mypy", returncode=None, has_error=False)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0)
    parsed = [json.loads(line) for line in lines]

    tool_record = parsed[0]
    assert tool_record["kind"] == "command"
    assert tool_record["status"] == "skipped"
    assert "rc" not in tool_record


# ---------------------------------------------------------------------------
# tool レコードの message フィールド
# ---------------------------------------------------------------------------


def test_command_record_message_on_failure_without_diagnostics(default_config):
    """status=failed かつ diagnostics=0 のとき、output 末尾が message に入ること。"""
    output = "line1\nline2\nError: command not found\n"
    result = _make_result("shellcheck", returncode=127, output=output)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    assert tool_record["status"] == "failed"
    assert "message" in tool_record
    assert "Error: command not found" in tool_record["message"]


def test_command_record_message_truncates_long_output(default_config):
    """長い output は末尾 30 行かつ 2000 文字にトリムされること。"""
    many_lines = "\n".join(f"line{i}" for i in range(100))
    result = _make_result("shellcheck", returncode=1, output=many_lines)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    msg = tool_record["message"]
    assert msg.startswith("... (truncated)")
    assert "line99" in msg
    assert "line70" in msg  # 末尾 30 行の範囲
    assert "line69" not in msg  # 範囲外
    assert len(msg) <= 2000 + len("... (truncated)\n")


def test_command_record_no_message_when_diagnostics_present(default_config):
    """failed でも diagnostics > 0 のときは message を出さない。"""
    errors = [_make_error("mypy", "src/a.py", 1, "bad")]
    result = _make_result("mypy", returncode=1, output="verbose mypy output", errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = next(json.loads(line) for line in lines if json.loads(line)["kind"] == "command")
    assert "message" not in tool_record


def test_command_record_no_message_on_success(default_config):
    """status=succeeded/formatted では message を出さない。"""
    ok = _make_result("mypy", returncode=0, output="all ok")
    fmt = _make_result("ruff-format", returncode=1, command_type="formatter", output="reformatted", has_error=False)
    lines = pyfltr.llm_output.build_lines([ok, fmt], default_config, exit_code=0)
    for line in lines:
        record = json.loads(line)
        if record["kind"] == "command":
            assert "message" not in record


def test_command_record_no_message_when_output_empty(default_config):
    """failed でも output が空なら message を出さない (キーごと省略)。"""
    result = _make_result("shellcheck", returncode=1, output="")
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    assert "message" not in tool_record


# ---------------------------------------------------------------------------
# structured_logger 経由の書き出し
# ---------------------------------------------------------------------------


def test_calculate_returncode_matches_summary_exit(default_config):
    """summary.exit と calculate_returncode の戻り値が一致すること。"""
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
# CLI 統合テスト (pyfltr.main.run)
# ---------------------------------------------------------------------------


def test_run_cli_jsonl_stdout_suppresses_text(mocker, capsys):
    """jsonl + stdout モードでは stdout は JSONL のみで text は stderr (WARN+) 扱いになる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    captured = capsys.readouterr()
    # stdout は JSONL のみ。text 整形の区切り線が混入しないこと。
    assert "----- pyfltr" not in captured.out
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONL が 1 行も出ていない"
    first = json.loads(lines[0])
    assert first["kind"] == "header"
    # 既定では commands 配列は出ず、commands_count が入る
    assert first["commands_count"] == 1
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"
    assert last["exit"] == 0
    # stderr には INFO 進捗・summary が出ない（jsonl stdout は WARN 以上）
    assert "----- pyfltr" not in captured.err
    assert "----- summary" not in captured.err


def test_run_cli_output_file_keeps_text_stdout(mocker, capsys, tmp_path):
    """--output-file 指定時は stdout には従来 text、ファイルには JSONL。"""
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
    # 従来の text 出力が stdout に出る
    assert "summary" in captured.out
    # ファイルには JSONL
    lines = destination.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["kind"] == "summary"


def test_run_cli_jsonl_ignores_ui(mocker, capsys):
    """jsonl + stdout モードでは --ui が silently 無効化される。stdout は JSONL のみ。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(
        ["ci", "--output-format=jsonl", "--ui", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0
    captured = capsys.readouterr()
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONL が 1 行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"


def test_run_cli_env_var_jsonl(mocker, capsys, monkeypatch):
    """PYFLTR_OUTPUT_FORMAT=jsonl で --output-format 未指定でも JSONL 出力になる。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "jsonl")

    returncode = pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    captured = capsys.readouterr()
    assert "----- pyfltr" not in captured.out
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONL が 1 行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"


def test_run_cli_env_var_overridden_by_cli(mocker, capsys, monkeypatch):
    """PYFLTR_OUTPUT_FORMAT より CLI --output-format=text が優先される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "jsonl")

    pyfltr.main.run(["ci", "--output-format=text", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    # CLI で text を明示しているので text 整形出力が stdout に出るべき
    assert "summary" in captured.out


def test_run_cli_env_var_invalid(monkeypatch):
    """PYFLTR_OUTPUT_FORMAT に不正値が入っている場合は SystemExit で終了する。"""
    # argparse の parents 共有によるデフォルト汚染を避けるため、_resolve_output_format を直接テストする。
    # （cli_value=None の場合のみ環境変数が参照される）
    monkeypatch.setenv("PYFLTR_OUTPUT_FORMAT", "yaml")
    parser = pyfltr.main.build_parser()
    with pytest.raises(SystemExit):
        pyfltr.main._resolve_output_format(parser, None)


def test_run_cli_jsonl_restores_logger_state(mocker, capsys):
    """jsonl モード実行後に text モードを再実行すると、text 出力が stdout に戻ること。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    # 1 回目: jsonl モード（stdout に JSONL、text は stderr の WARN+）
    pyfltr.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    # 1 回目の stdout は読み捨てる (capsys をリセット)
    capsys.readouterr()

    # 2 回目: text モード (従来どおりのログが出るべき)。
    pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    captured = capsys.readouterr()
    assert "summary" in captured.out


# ---------------------------------------------------------------------------
# build_command_lines のユニットテスト
# ---------------------------------------------------------------------------


def test_build_command_lines_with_diagnostics(default_config):
    """diagnostic行+tool行がツール単位で(command, file)集約されてまとまること。"""
    errors = [
        _make_error("mypy", "src/b.py", 5, "later"),
        _make_error("mypy", "src/a.py", 10, "earlier"),
    ]
    result = _make_result("mypy", returncode=1, errors=errors)
    lines = pyfltr.llm_output.build_command_lines(result, default_config)
    parsed = [json.loads(line) for line in lines]

    assert len(parsed) == 3
    # diagnostic行はツール内でファイル順にソートされる（src/a.py → src/b.py）
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
    """サニタイズ対象文字を含むcommand名でも``truncated.archive``は実保存キーと一致する。

    ``archive.ArchiveStore.write_tool_result``が書き込む保存キーと
    ``command.truncated.archive``が参照するパスが同じサニタイズ関数を通ることを検証する。
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
# write_jsonl_streaming のユニットテスト
# ---------------------------------------------------------------------------


def _configure_structured_stdout() -> None:
    """テストのため structured_logger を現在の ``sys.stdout`` に向ける。

    ``capsys`` フィクスチャは ``sys.stdout`` を差し替えているため、呼び出し時点の
    ``sys.stdout`` をそのまま StreamHandler に掴ませれば capsys で拾える。
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
# write_jsonl_footer のユニットテスト
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
    assert parsed[0]["succeeded"] == 1


# ---------------------------------------------------------------------------
# header レコードのユニットテスト
# ---------------------------------------------------------------------------


def test_build_header_record_fields():
    """_build_header_record が必要なフィールドをすべて含むこと (既定は commands_count)。"""
    record = pyfltr.llm_output._build_header_record(["ruff-format", "mypy"], 42)
    assert record["kind"] == "header"
    # 既定では commands_count (整数) のみ。フル配列は verbose=True で出る。
    assert "commands" not in record
    assert record["commands_count"] == 2
    assert record["files"] == 42
    assert "version" in record
    assert "python" in record
    assert "executable" in record
    assert "platform" in record
    assert "cwd" in record


def test_build_lines_header_first(default_config):
    """commands/filesを指定するとheader行が先頭に出力されること (既定は commands_count)。"""
    result = _make_result("mypy", returncode=0)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0, commands=["mypy"], files=10)
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["kind"] == "header"
    assert parsed[0]["commands_count"] == 1
    assert parsed[0]["files"] == 10
    assert parsed[-1]["kind"] == "summary"


def test_build_lines_header_verbose_has_full_commands(default_config):
    """verbose=Trueでheader行にフルcommands配列とフルschema_hintsが出ること。"""
    result = _make_result("mypy", returncode=0)
    lines = pyfltr.llm_output.build_lines(
        [result], default_config, exit_code=0, commands=["mypy", "ruff-check"], files=10, verbose=True
    )
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["kind"] == "header"
    assert parsed[0]["commands"] == ["mypy", "ruff-check"]
    assert "commands_count" not in parsed[0]
    assert "diagnostic.messages" in parsed[0]["schema_hints"]


def test_build_lines_no_header_when_omitted(default_config):
    """commands/filesを省略するとheader行は出力されないこと。"""
    result = _make_result("mypy", returncode=0)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=0)
    parsed = [json.loads(line) for line in lines]
    assert all(r["kind"] != "header" for r in parsed)


def test_write_jsonl_header_stdout(capsys):
    """write_jsonl_headerがstdoutにheader行を書き出すこと (既定は commands_count)。"""
    _configure_structured_stdout()
    pyfltr.llm_output.write_jsonl_header(commands=["ruff-format", "mypy"], files=5)
    captured = capsys.readouterr()
    parsed = [json.loads(line) for line in captured.out.splitlines()]
    assert len(parsed) == 1
    assert parsed[0]["kind"] == "header"
    assert parsed[0]["commands_count"] == 2
    assert parsed[0]["files"] == 5


def test_write_jsonl_header_stdout_verbose(capsys):
    """write_jsonl_header の verbose=True でフル commands 配列と schema_hints が出る。"""
    _configure_structured_stdout()
    pyfltr.llm_output.write_jsonl_header(commands=["ruff-format", "mypy"], files=5, verbose=True)
    captured = capsys.readouterr()
    parsed = [json.loads(line) for line in captured.out.splitlines()]
    assert parsed[0]["commands"] == ["ruff-format", "mypy"]
    assert "diagnostic.messages" in parsed[0]["schema_hints"]


# ---------------------------------------------------------------------------
# code-quality 形式の CLI 統合テスト
# ---------------------------------------------------------------------------


_REQUIRED_CQ_FIELDS = {"description", "check_name", "fingerprint", "severity", "location"}


def test_run_cli_code_quality_stdout(mocker, capsys):
    """code-quality + stdout モードでは stdout は JSON 配列、stderr に text 整形が出る。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    returncode = pyfltr.main.run(
        ["ci", "--output-format=code-quality", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0
    captured = capsys.readouterr()
    # stdout は JSON 配列 1 件
    payload = json.loads(captured.out)
    assert isinstance(payload, list)
    # code-quality は診断なしなら空配列だが、ルート型が list である確認が主眼
    for issue in payload:
        assert issue.keys() >= _REQUIRED_CQ_FIELDS
        assert (issue.keys() | {"location"}) >= _REQUIRED_CQ_FIELDS
        assert {"path", "lines"} <= issue["location"].keys()
        assert "begin" in issue["location"]["lines"]
    # stderr には text 整形（進捗・summary）が出る
    assert "----- pyfltr" in captured.err
    assert "----- summary" in captured.err


def test_run_cli_code_quality_output_file(mocker, capsys, tmp_path):
    """code-quality + --output-file ではファイルに JSON 配列、stdout に text 整形。"""
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
    # stdout には従来の text 整形出力
    assert "summary" in captured.out
    # ファイルは JSON 配列
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert isinstance(payload, list)


def test_run_cli_code_quality_with_diagnostics(mocker, capsys):
    """エラーを検出したツールで Code Quality 必須フィールドを満たす issue が出る。"""
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
