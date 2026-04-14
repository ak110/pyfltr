# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=protected-access

import json
import logging
import pathlib
import subprocess

import pytest

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
    """error_parser 対応ツールの診断が diagnostic レコードとして出ること。"""
    errors = [
        _make_error("mypy", "src/a.py", 10, "bad type", col=4),
        _make_error("mypy", "src/a.py", 20, "missing return"),
    ]
    result = _make_result("mypy", returncode=1, errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    parsed = [json.loads(line) for line in lines]

    assert [r["kind"] for r in parsed] == ["diagnostic", "diagnostic", "tool", "summary"]
    assert parsed[0] == {
        "kind": "diagnostic",
        "tool": "mypy",
        "file": "src/a.py",
        "line": 10,
        "col": 4,
        "msg": "bad type",
    }
    assert parsed[1] == {
        "kind": "diagnostic",
        "tool": "mypy",
        "file": "src/a.py",
        "line": 20,
        "msg": "missing return",
    }
    assert parsed[2]["diagnostics"] == 2
    assert parsed[2]["status"] == "failed"
    assert parsed[3]["diagnostics"] == 2
    assert parsed[3]["failed"] == 1


def test_build_lines_unsupported_tool_only(default_config):
    """error_parser 非対応ツール (black) は tool レコードのみ。"""
    result = _make_result("black", returncode=1, command_type="formatter", has_error=False)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    parsed = [json.loads(line) for line in lines]

    assert [r["kind"] for r in parsed] == ["tool", "summary"]
    assert parsed[0]["tool"] == "black"
    assert parsed[0]["status"] == "formatted"
    assert parsed[0]["diagnostics"] == 0
    assert "message" not in parsed[0]
    assert parsed[1]["formatted"] == 1


def test_build_lines_mixed_order(default_config):
    """diagnostic はファイル/行順、tool は config.command_names 順になること。"""
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
    black_result = _make_result("black", returncode=0, command_type="formatter")

    # config.command_names 順では black → mypy → pylint
    lines = pyfltr.llm_output.build_lines([mypy_result, pylint_result, black_result], default_config, exit_code=1)
    parsed = [json.loads(line) for line in lines]

    diagnostic_records = [r for r in parsed if r["kind"] == "diagnostic"]
    assert [(r["file"], r["line"], r["tool"]) for r in diagnostic_records] == [
        ("src/a.py", 10, "pylint"),
        ("src/a.py", 30, "mypy"),
        ("src/b.py", 5, "mypy"),
    ]

    tool_records = [r for r in parsed if r["kind"] == "tool"]
    assert [r["tool"] for r in tool_records] == ["black", "mypy", "pylint"]


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
    assert tool_record["kind"] == "tool"
    assert tool_record["status"] == "skipped"
    assert "rc" not in tool_record


# ---------------------------------------------------------------------------
# tool レコードの message フィールド
# ---------------------------------------------------------------------------


def test_tool_record_message_on_failure_without_diagnostics(default_config):
    """status=failed かつ diagnostics=0 のとき、output 末尾が message に入ること。"""
    output = "line1\nline2\nError: command not found\n"
    result = _make_result("shellcheck", returncode=127, output=output)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    assert tool_record["status"] == "failed"
    assert "message" in tool_record
    assert "Error: command not found" in tool_record["message"]


def test_tool_record_message_truncates_long_output(default_config):
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


def test_tool_record_no_message_when_diagnostics_present(default_config):
    """failed でも diagnostics > 0 のときは message を出さない。"""
    errors = [_make_error("mypy", "src/a.py", 1, "bad")]
    result = _make_result("mypy", returncode=1, output="verbose mypy output", errors=errors)
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = next(json.loads(line) for line in lines if json.loads(line)["kind"] == "tool")
    assert "message" not in tool_record


def test_tool_record_no_message_on_success(default_config):
    """status=succeeded/formatted では message を出さない。"""
    ok = _make_result("mypy", returncode=0, output="all ok")
    fmt = _make_result("black", returncode=1, command_type="formatter", output="reformatted", has_error=False)
    lines = pyfltr.llm_output.build_lines([ok, fmt], default_config, exit_code=0)
    for line in lines:
        record = json.loads(line)
        if record["kind"] == "tool":
            assert "message" not in record


def test_tool_record_no_message_when_output_empty(default_config):
    """failed でも output が空なら message を出さない (キーごと省略)。"""
    result = _make_result("shellcheck", returncode=1, output="")
    lines = pyfltr.llm_output.build_lines([result], default_config, exit_code=1)
    tool_record = json.loads(lines[0])
    assert "message" not in tool_record


# ---------------------------------------------------------------------------
# write_jsonl の出力先
# ---------------------------------------------------------------------------


def test_write_jsonl_stdout(default_config, capsys):
    """destination=None のとき sys.stdout に書き出す。"""
    result = _make_result("mypy", returncode=0)
    pyfltr.llm_output.write_jsonl([result], default_config, exit_code=0, destination=None)
    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = [json.loads(line) for line in captured.out.splitlines()]
    assert parsed[-1]["kind"] == "summary"
    assert parsed[-1]["succeeded"] == 1


def test_write_jsonl_file_creates_parent(default_config, tmp_path):
    """destination 指定時、親ディレクトリが自動作成される。"""
    destination = tmp_path / "sub" / "dir" / "out.jsonl"
    result = _make_result("mypy", returncode=0)
    pyfltr.llm_output.write_jsonl([result], default_config, exit_code=0, destination=destination)
    assert destination.exists()
    content = destination.read_text(encoding="utf-8")
    parsed = [json.loads(line) for line in content.splitlines()]
    assert parsed[-1]["kind"] == "summary"


def test_calculate_returncode_matches_summary_exit(default_config):
    """summary.exit と calculate_returncode の戻り値が一致すること。"""
    results = [
        _make_result("mypy", returncode=1, errors=[_make_error("mypy", "a.py", 1, "bad")]),
        _make_result("black", returncode=0, command_type="formatter"),
    ]
    exit_code = pyfltr.main.calculate_returncode(results, exit_zero_even_if_formatted=False)
    lines = pyfltr.llm_output.build_lines(results, default_config, exit_code=exit_code)
    summary = json.loads(lines[-1])
    assert summary["exit"] == exit_code == 1


# ---------------------------------------------------------------------------
# CLI 統合テスト (pyfltr.main.run)
# ---------------------------------------------------------------------------


def test_run_cli_jsonl_stdout_suppresses_text(mocker, capsys):
    """jsonl + stdout モードでは stdout は JSONL のみで text ログは出ない。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    captured = capsys.readouterr()
    assert "----- pyfltr" not in captured.out
    assert "summary" not in captured.out or '"kind":"summary"' in captured.out
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONL が 1 行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"
    assert last["exit"] == 0


def test_run_cli_output_file_keeps_text_stdout(mocker, caplog, tmp_path):
    """--output-file 指定時は stdout には従来 text、ファイルには JSONL。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("subprocess.run", return_value=proc)

    destination = tmp_path / "out.jsonl"
    with caplog.at_level(logging.INFO):
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
    # 従来の text ログが logging 経由で出ている
    assert "summary" in caplog.text
    # ファイルには JSONL
    lines = destination.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["kind"] == "summary"


def test_run_cli_jsonl_ignores_ui(mocker, capsys):
    """jsonl + stdout モードでは --ui が silently 無効化される (stderr に漏れない)。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(
        ["ci", "--output-format=jsonl", "--ui", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = [line for line in captured.out.splitlines() if line.strip()]
    assert lines, "JSONL が 1 行も出ていない"
    last = json.loads(lines[-1])
    assert last["kind"] == "summary"


def test_run_cli_jsonl_restores_logger_state(mocker, caplog, capsys):
    """jsonl モード実行後、text モードの logger が復元されること。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy ok")
    mocker.patch("subprocess.run", return_value=proc)

    # 1 回目: jsonl モード (logger 抑止)
    pyfltr.main.run(["ci", "--output-format=jsonl", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    # 1 回目の stdout は読み捨てる (capsys をリセット)
    capsys.readouterr()
    caplog.clear()

    # 2 回目: text モード (従来どおりのログが出るべき)
    with caplog.at_level(logging.INFO):
        pyfltr.main.run(["ci", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])

    assert "summary" in caplog.text
