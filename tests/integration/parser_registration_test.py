"""組み込みパーサーの登録・特殊パス・親子の帰属とCLI・MCP診断を検査する。"""

import asyncio
import json
import pathlib
import subprocess
import sys

import pytest

import pyfltr.cli.mcp_server
import pyfltr.command.slow_tests
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.paths
import pyfltr.state.archive
import pyfltr.tools
from tests.parsing.native_test import EXPECTED, OUTPUTS

_FIXTURES = json.loads(
    pathlib.Path(__file__).parents[1].joinpath("parsing/data/registered_outputs.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("file", ["sp ace_test.py", "odd:name.py", "scripttool", "gonescript"])
def test_restore_pytest_subproject_positions_without_basis(file: str) -> None:
    """保存出力の再解析は元ファイルの実在を要求せずサブプロジェクトへ戻す。"""
    test_file = file if " " in file or ":" in file else "caller_test.py"
    function = "test_script" if test_file == file else "boom"
    body = (
        f"===== FAILURES =====\n_____ test_script _____\n{file}:2: in {function}\n    ???\nE   ValueError: gone\n"
        f"===== short test summary info =====\nFAILED {test_file}::test_script - ValueError: gone\n"
    )
    entries = [{"file": file, "messages": [{"line": 2, "msg": "test_script: ValueError: gone"}]}]
    restored = pyfltr.state.archive.restore_subproject_paths("pytest", "# subproject: pkg\n" + body, entries)
    assert restored == [{**entries[0], "file": "pkg/" + file}]


@pytest.mark.parametrize(
    "command", [name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.type in ("linter", "tester")]
)
def test_builtin_linters_and_testers_have_parser_and_failure_sample(command: str) -> None:
    """全定義へ解析手段と失敗検体を要求し、診断の欠落を登録時に検出する。"""
    info = pyfltr.tools.BUILTIN_COMMANDS[command]
    assert info.error_pattern or info.diagnostic_pattern or info.parser or info.context_parser or info.path_resolving_parser, (
        f"{command}: tools.pyへ解析手段を登録し、registered_outputs.jsonへ位置付き失敗の検体を追加してください"
    )
    assert command in _FIXTURES, f"{command}: registered_outputs.jsonへ実出力に基づく失敗検体を追加してください"
    fixture = _FIXTURES[command]
    errors = pyfltr.parsing.entry.parse_errors(command, fixture["output"], error_pattern=info.error_pattern)
    if fixture["reports_line"]:
        assert any(error.file and error.line > 0 for error in errors), (
            f"{command}: 位置付き失敗の検体から診断を得られません。解析手段と検体を修正してください"
        )
    else:
        # 行を報告しないツールも、実出力の構造と理由を検査してファイル診断を保持する。
        finding = json.loads(fixture["output"])["findings"]
        assert fixture["position_reason"]
        assert finding and all("line" not in item and "location" not in item for item in finding)
        assert errors and all(error.file and error.line == 0 for error in errors)


@pytest.mark.parametrize("command", EXPECTED)
def test_native_output_reaches_jsonl_and_mcp(command: str, tmp_path: pathlib.Path) -> None:
    """実行ファイルだけをスタブへ差し替え、登録・解析・保存・返却の経路を通す。"""
    filename, expected_line = EXPECTED[command]
    target = tmp_path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sample\n", encoding="utf-8")
    output = OUTPUTS[command]["output"]
    script = f"import sys; sys.stdout.write({output!r}); sys.exit(1)"
    config = (
        f"[tool.pyfltr]\n{command} = true\nrespect-gitignore = false\n"
        f"{command}-path = {json.dumps(sys.executable)}\n{command}-args = {json.dumps(['-c', script])}\n"
        f"{command}-targets = ['*']\n"
    )
    (tmp_path / "pyproject.toml").write_text(config, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyfltr",
            "run",
            "--work-dir",
            str(tmp_path),
            f"--commands={command}",
            "--no-fix",
            "--no-cache",
            "--output-format=jsonl",
            filename,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        check=False,
        timeout=45,
        cwd=tmp_path,
    )
    assert proc.returncode == 1, proc.stderr + proc.stdout
    records = [json.loads(line) for line in proc.stdout.splitlines() if line]
    diagnostic = next(item for item in records if item.get("kind") == "diagnostic")
    assert diagnostic["file"] == filename
    assert diagnostic["messages"][0]["line"] == expected_line
    run_id = next(item["run_id"] for item in records if item.get("kind") == "header")
    result = asyncio.run(pyfltr.cli.mcp_server.tool_show_run_diagnostics(run_id, [command]))
    assert result[0].diagnostics[0].file == filename
    assert result[0].diagnostics[0].messages[0].line == expected_line
    if command == "gitleaks":
        assert '"Secret": "REDACTED"' in output


@pytest.mark.parametrize("file", ["normal.py", "bad path.py", "bad:colon.py", "a:10:20/name.py", r"C:\my project\sample.py"])
@pytest.mark.parametrize(
    "command,suffix,line,col,rule,severity",
    [
        ("yamllint", ":3:4: [error] unexpected value (syntax)", 3, 4, "syntax", "error"),
        ("mypy", ":3: error: bad value [assignment]", 3, None, "assignment", None),
        ("ty", ":3:4: error[invalid-type] bad value", 3, 4, "invalid-type", "error"),
        ("markdownlint", ":3:4 MD001/heading-increment bad value", 3, 4, "MD001", None),
        ("ec", ":3:4: error: bad value", 3, 4, None, None),
        ("actionlint", ":3:4: bad value [expression]", 3, 4, "expression", None),
        ("colloquial-check", ":3:4: bad value", 3, 4, None, None),
        ("pylint", ":3:4: E0001: bad value (syntax-error)", 3, 4, None, None),
        ("ruff-check", ":3:4: F401 bad value", 3, 4, None, None),
        ("pyright", ":3:4 - error: bad value", 3, 4, None, None),
        ("textlint", ": line 3, col 4, Error - bad value", 3, 4, None, None),
        ("shellcheck", ":3:4: error: bad value", 3, 4, None, None),
        ("typos", ":3:4: bad value", 3, 4, None, None),
    ],
)
def test_builtin_position_delimiters(
    command: str, suffix: str, line: int, col: int | None, rule: str | None, severity: str | None, file: str
) -> None:
    """各ツールのテキスト経路でパス全体と位置・本文を保つ。"""
    pattern = pyfltr.tools.BUILTIN_COMMANDS[command].require_diagnostic_pattern()
    errors = pyfltr.parsing.entry.parse_errors(command, file + suffix, error_pattern=pattern)
    assert len(errors) == 1
    error = errors[0]
    assert (error.file, error.line, error.col, error.rule, error.severity) == (
        pyfltr.paths.normalize_separators(file),
        line,
        col,
        rule,
        severity,
    )
    assert "bad value" in error.message or "unexpected value" in error.message


@pytest.mark.parametrize(
    "file", ["normal_test.py", "bad path_test.py", "bad:colon_test.py", "bad:12:colon_test.py", r"C:\my tests\sample_test.py"]
)
@pytest.mark.parametrize("style", ["summary", "line", "location", "frame", "crash"])
@pytest.mark.parametrize(
    "noise", ["E   ValueError: bad time 10:20:30", ">       x[1:3:2]", "E   text:99: in noise", ">   text:99: in noise"]
)
def test_pytest_position_formats_preserve_filename(file: str, style: str, noise: str, tmp_path: pathlib.Path) -> None:
    """pytestの5形式で同じファイルとテスト名を保ち、重複させない。"""
    summary = f"FAILED {file}::TestCase::test_sample - AssertionError: sample\n"
    if style == "summary":
        output = summary
    elif style == "line":
        location = file if pathlib.PureWindowsPath(file).is_absolute() else (tmp_path / file).as_posix()
        output = (
            f"===== FAILURES =====\nE   AssertionError: sample\n{location}:3: AssertionError: sample\n"
            f"{noise}\n===== short test summary info =====\n" + summary
        )
    else:
        details = {
            "location": f"    def test_sample():\n>       assert False\nE   AssertionError: sample\n{file}:3: AssertionError\n",
            "frame": f"{file}:3: in test_sample\n    assert False\nE   AssertionError: sample\n",
            "crash": f"worker 'gw0' crashed while running '{file}::TestCase::test_sample'\n",
        }
        output = "===== FAILURES =====\n_____ TestCase.test_sample _____\n" + details[style] + noise + "\n"
        output += "===== short test summary info =====\n" + summary
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)
    assert len(errors) == 1
    assert errors[0].file == pyfltr.paths.normalize_separators(file)
    assert errors[0].line == (0 if style in ("summary", "crash") else 3)
    assert "test_sample" in errors[0].message


def test_user_error_pattern_keeps_inline_search() -> None:
    """利用者指定のパターンは行内検索を保つ。"""
    errors = pyfltr.parsing.entry.parse_errors(
        "custom", "prefix sample.py:3: failure", error_pattern=r"(?P<file>sample.py):(?P<line>\d+): (?P<message>.+)"
    )
    assert len(errors) == 1
    assert (errors[0].file, errors[0].line) == ("sample.py", 3)


_PYTEST_RUN_OUTPUTS = pathlib.Path(__file__).parents[1] / "parsing/data/pytest_runs.json"


@pytest.mark.parametrize(
    ("form", "expected"),
    [
        ("exception-group-auto", {"test_group": ("y_test.py", 10)}),
        (
            "runpy-extensionless-auto",
            {"test_extensionless_script": ("scripttool", 2), "test_exec_string": ("runpy_test.py", 15)},
        ),
        ("showlocals-short", {"test_arg_location_like": ("z_test.py", 2), "test_local_location_like": ("z_test.py", 12)}),
    ],
)
def test_pytest_mixed_traceback_real_positions(form: str, expected: dict[str, tuple[str, int]], tmp_path: pathlib.Path) -> None:
    """標準形式の混在・拡張子なし実ファイル・showlocalsの実出力を検証する。"""
    output = json.loads(_PYTEST_RUN_OUTPUTS.read_text(encoding="utf-8"))[form]
    (tmp_path / "scripttool").write_text("raise ValueError('from script')\n", encoding="utf-8")
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)
    for name, position in expected.items():
        matched = [error for error in errors if error.message.startswith(name + ":")]
        assert len(matched) == 1
        assert (matched[0].file, matched[0].line) == position


@pytest.mark.parametrize("form", ["normal", "quiet", "xdist", "s", "q-s", "qq-s", "q-s-captured", "q-s-parent-durations"])
def test_pytest_parent_owns_only_its_diagnostics_and_metadata(form: str) -> None:
    """親の実行形態によらず、子の競合・所要時間・失敗を親へ帰属させない。"""
    # scripts.parser_review pytest-formsでpytest 9.1.1の実出力を採取した検体。
    output = json.loads(_PYTEST_RUN_OUTPUTS.read_text(encoding="utf-8"))[form]
    assert pyfltr.parsing.pytest.detect_pytest_config_conflict(output) is None
    durations = pyfltr.command.slow_tests.parse_pytest_durations(output)
    if form == "q-s-parent-durations":
        assert durations
        assert all("parent_test.py::test_parent" in item.nodeid for item in durations)
    else:
        assert not durations
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert len(errors) == 1
    assert (errors[0].file, errors[0].line) == ("parent_test.py", 9)
    assert "test_parent" in errors[0].message


@pytest.mark.parametrize(("form", "line"), [("short-time", 2), ("auto-external-time", 5)])
def test_pytest_real_time_failure_has_project_position(form: str, line: int) -> None:
    """実出力にある例外本文と引数表示の時刻を位置と誤認しない。"""
    output = json.loads(_PYTEST_RUN_OUTPUTS.read_text(encoding="utf-8"))[form]
    errors = pyfltr.parsing.entry.parse_errors("pytest", output)
    assert [(error.file, error.line) for error in errors] == [("time_test.py", line)]


@pytest.mark.parametrize("form", ["native-body", "doctest-files"])
def test_pytest_native_and_doctest_real_positions(form: str, tmp_path: pathlib.Path) -> None:
    """nativeの一覧補完と任意拡張子のdoctest位置を実出力で維持する。"""
    output = json.loads(_PYTEST_RUN_OUTPUTS.read_text(encoding="utf-8"))[form]
    output = output.replace("__SAMPLE_DIR__", tmp_path.as_posix())
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=tmp_path, start_cwd=tmp_path)
    if form == "native-body":
        assert len(errors) == 3
        assert all((error.file, error.line) == ("time_native_test.py", 0) for error in errors)
        assert all("Value" in error.message for error in errors)
    else:
        assert {(error.file, error.line) for error in errors} == {
            ("normal.txt", 1),
            ("bad path:sample.rst", 1),
            ("sample.custom", 1),
            ("extensionless", 1),
        }
