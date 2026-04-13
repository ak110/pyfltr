"""error_parserのテストコード。"""

import json
import pathlib

import pytest

import pyfltr.error_parser


@pytest.mark.parametrize(
    "command,output,expected_count,expected_first_file,expected_first_line",
    [
        # mypy
        (
            "mypy",
            'src/foo.py:10: error: Name "x" is not defined  [name-defined]\nsrc/bar.py:20: error: Missing return  [return]',
            2,
            "src/foo.py",
            10,
        ),
        # pylint
        (
            "pylint",
            "src/foo.py:10:5: C0114: Missing module docstring (missing-module-docstring)",
            1,
            "src/foo.py",
            10,
        ),
        # ruff-check
        (
            "ruff-check",
            "src/foo.py:10:5: F401 `os` imported but unused\nsrc/bar.py:3:1: E302 Expected 2 blank lines",
            2,
            "src/foo.py",
            10,
        ),
        # pyright
        (
            "pyright",
            '  src/foo.py:10:5 - error: Type "int" is not assignable',
            1,
            "src/foo.py",
            10,
        ),
        # markdownlint-cli2
        (
            "markdownlint",
            "docs/index.md:3 MD001/heading-increment Heading levels should only increment by one level at a time",
            1,
            "docs/index.md",
            3,
        ),
        # textlint --format compact
        (
            "textlint",
            "docs/index.md: line 5, col 1, Error - sentence error (ja-technical-writing/ja-no-mixed-period)",
            1,
            "docs/index.md",
            5,
        ),
        # ty check --output-format concise (error)
        (
            "ty",
            "src/foo.py:10:5: error[invalid-argument-type] Argument is incorrect",
            1,
            "src/foo.py",
            10,
        ),
        # ty check --output-format concise (warning)
        (
            "ty",
            "src/foo.py:3:1: warning[unused-variable] Variable `x` is unused",
            1,
            "src/foo.py",
            3,
        ),
        # pytest
        (
            "pytest",
            "FAILED tests/foo_test.py::test_bar - AssertionError: xxx",
            1,
            "tests/foo_test.py",
            0,  # pytestはline情報なし
        ),
        # biome --reporter=github (line と col の間に endLine が挟まる)
        (
            "biome",
            "::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,"
            "line=1,endLine=1,col=7,endColumn=9::Use === instead of ==",
            1,
            "src/foo.ts",
            1,
        ),
        # biome --reporter=github (warning)
        (
            "biome",
            "::warning title=lint/style/useConst,file=src/bar.ts,line=5,endLine=5,col=3,endColumn=6::Use const instead of let",
            1,
            "src/bar.ts",
            5,
        ),
        # パースできないコマンド
        (
            "unknown",
            "some output",
            0,
            None,
            None,
        ),
    ],
)
def test_parse_errors(
    command: str,
    output: str,
    expected_count: int,
    expected_first_file: str | None,
    expected_first_line: int | None,
) -> None:
    """ビルトインパーサーのテスト。"""
    errors = pyfltr.error_parser.parse_errors(command, output)
    assert len(errors) == expected_count
    if expected_count > 0:
        assert errors[0].file == expected_first_file
        assert errors[0].line == expected_first_line
        assert errors[0].command == command


def test_parse_errors_eslint_json() -> None:
    """ESLint --format json 出力のパース。"""
    output = json.dumps(
        [
            {
                "filePath": str(pathlib.Path.cwd() / "src" / "foo.js"),
                "messages": [
                    {
                        "line": 10,
                        "column": 5,
                        "message": "'x' is defined but never used.",
                        "ruleId": "no-unused-vars",
                        "severity": 2,
                    },
                    {
                        "line": 20,
                        "column": 1,
                        "message": "Missing semicolon.",
                        "ruleId": "semi",
                        "severity": 2,
                    },
                ],
            },
            {
                "filePath": str(pathlib.Path.cwd() / "src" / "bar.js"),
                "messages": [],
            },
        ]
    )
    errors = pyfltr.error_parser.parse_errors("eslint", output)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.js"  # cwd 配下は相対パスに正規化される
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert "no-unused-vars" in errors[0].message
    assert errors[0].command == "eslint"
    assert errors[1].line == 20


def test_parse_errors_eslint_json_empty_array() -> None:
    """空配列 `[]` は空リストを返す。"""
    errors = pyfltr.error_parser.parse_errors("eslint", "[]")
    assert errors == []


def test_parse_errors_eslint_json_empty_string() -> None:
    """空文字列は空リストを返す (例外なし)。"""
    errors = pyfltr.error_parser.parse_errors("eslint", "")
    assert errors == []


def test_parse_errors_eslint_json_invalid() -> None:
    """不正な JSON (stderr 混入等) は空リストを返す。"""
    errors = pyfltr.error_parser.parse_errors("eslint", "Warning: something\n[not json]")
    assert errors == []


def test_parse_errors_eslint_json_no_rule_id() -> None:
    """ruleId が null の場合でも message のみ格納する。"""
    output = json.dumps(
        [
            {
                "filePath": "/abs/src/foo.js",
                "messages": [
                    {
                        "line": 1,
                        "column": 1,
                        "message": "Parsing error",
                        "ruleId": None,
                        "severity": 2,
                    },
                ],
            }
        ]
    )
    errors = pyfltr.error_parser.parse_errors("eslint", output)
    assert len(errors) == 1
    assert errors[0].message == "Parsing error"


def test_parse_errors_custom_pattern() -> None:
    """カスタムerror-patternのテスト。"""
    pattern = r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)"
    output = "src/foo.py:10:5: some error\nsrc/bar.py:20:3: another error"
    errors = pyfltr.error_parser.parse_errors("custom-tool", output, error_pattern=pattern)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert errors[0].message == "some error"
    assert errors[1].file == "src/bar.py"


def test_sort_errors() -> None:
    """エラーソートのテスト。"""
    command_names = ["ruff-check", "mypy", "pylint"]
    errors = [
        pyfltr.error_parser.ErrorLocation(file="src/bar.py", line=10, col=None, command="mypy", message="err1"),
        pyfltr.error_parser.ErrorLocation(file="src/bar.py", line=10, col=None, command="ruff-check", message="err2"),
        pyfltr.error_parser.ErrorLocation(file="src/foo.py", line=5, col=None, command="mypy", message="err3"),
    ]
    sorted_errors = pyfltr.error_parser.sort_errors(errors, command_names)

    # ファイル名でソート → 同一箇所はcommand_names順
    assert sorted_errors[0].file == "src/bar.py"
    assert sorted_errors[0].command == "ruff-check"  # command_namesで先
    assert sorted_errors[1].file == "src/bar.py"
    assert sorted_errors[1].command == "mypy"
    assert sorted_errors[2].file == "src/foo.py"


def test_parse_errors_normalizes_absolute_path() -> None:
    """絶対パスが相対パスに正規化されることのテスト。"""
    cwd = str(pathlib.Path.cwd())
    # pyright風の絶対パス出力
    output = f"  {cwd}/src/foo.py:10:5 - error: some type error"
    errors = pyfltr.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"  # 相対パスになっている


def test_format_error() -> None:
    """エラーフォーマットのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(file="src/foo.py", line=10, col=5, command="mypy", message="some error")
    assert pyfltr.error_parser.format_error(error) == "src/foo.py:10:5: [mypy] some error"

    # colなし
    error_no_col = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py", line=10, col=None, command="ruff-check", message="another error"
    )
    assert pyfltr.error_parser.format_error(error_no_col) == "src/foo.py:10: [ruff-check] another error"

    # ruleあり
    error_with_rule = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py", line=10, col=5, command="ruff-check", message="`os` imported but unused", rule="F401"
    )
    assert pyfltr.error_parser.format_error(error_with_rule) == "src/foo.py:10:5: [ruff-check:F401] `os` imported but unused"


def test_parse_ruff_check_json() -> None:
    """ruff check --output-format=json 出力のパース。"""
    output = json.dumps(
        [
            {
                "code": "F401",
                "message": "`os` imported but unused",
                "filename": "src/foo.py",
                "location": {"row": 1, "column": 8},
                "end_location": {"row": 1, "column": 10},
                "severity": "error",
                "fix": {"applicability": "safe", "edits": []},
            },
        ]
    )
    errors = pyfltr.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 1
    assert errors[0].col == 8
    assert errors[0].rule == "F401"
    assert errors[0].severity == "error"
    assert errors[0].fix == "safe"
    assert errors[0].message == "`os` imported but unused"


def test_parse_ruff_check_json_fallback() -> None:
    """ruff-check: JSON でない出力は regex にフォールバックする。"""
    output = "src/foo.py:10:5: F401 `os` imported but unused"
    errors = pyfltr.error_parser.parse_errors("ruff-check", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10


def test_parse_pylint_json() -> None:
    """pylint --output-format=json2 出力のパース。"""
    output = json.dumps(
        {
            "messages": [
                {
                    "messageId": "C0114",
                    "symbol": "missing-module-docstring",
                    "message": "Missing module docstring",
                    "path": "src/foo.py",
                    "line": 1,
                    "column": 0,
                    "type": "convention",
                },
            ],
            "statistics": {},
        }
    )
    errors = pyfltr.error_parser.parse_errors("pylint", output)
    assert len(errors) == 1
    assert errors[0].rule == "C0114"
    assert errors[0].severity == "warning"
    assert errors[0].message == "Missing module docstring"


def test_parse_pylint_json_fallback() -> None:
    """pylint: JSON でない出力は regex にフォールバックする。"""
    output = "src/foo.py:10:5: C0114: Missing module docstring (missing-module-docstring)"
    errors = pyfltr.error_parser.parse_errors("pylint", output)
    assert len(errors) == 1
    assert errors[0].line == 10


def test_parse_pyright_json() -> None:
    """pyright --outputjson 出力のパース。"""
    output = json.dumps(
        {
            "version": "1.1.400",
            "generalDiagnostics": [
                {
                    "file": "src/foo.py",
                    "range": {"start": {"line": 9, "character": 4}, "end": {"line": 9, "character": 10}},
                    "severity": "error",
                    "rule": "reportAssignmentType",
                    "message": "Type mismatch",
                },
            ],
            "summary": {"errorCount": 1},
        }
    )
    errors = pyfltr.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].line == 10  # 0-based → 1-based
    assert errors[0].col == 5  # 0-based → 1-based
    assert errors[0].rule == "reportAssignmentType"
    assert errors[0].severity == "error"


def test_parse_pyright_json_fallback() -> None:
    """pyright: JSON でない出力は regex にフォールバックする。"""
    output = '  src/foo.py:10:5 - error: Type "int" is not assignable'
    errors = pyfltr.error_parser.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].line == 10


def test_parse_shellcheck_json() -> None:
    """shellcheck -f json 出力のパース。"""
    output = json.dumps(
        [
            {
                "file": "src/foo.sh",
                "line": 10,
                "column": 5,
                "level": "warning",
                "code": 2086,
                "message": "Double quote to prevent globbing",
            },
        ]
    )
    errors = pyfltr.error_parser.parse_errors("shellcheck", output)
    assert len(errors) == 1
    assert errors[0].rule == "SC2086"
    assert errors[0].severity == "warning"
    assert errors[0].message == "Double quote to prevent globbing"


def test_parse_textlint_json() -> None:
    """textlint --format json 出力のパース。"""
    output = json.dumps(
        [
            {
                "filePath": "docs/index.md",
                "messages": [
                    {
                        "line": 5,
                        "column": 1,
                        "message": "文末が不統一です。",
                        "ruleId": "ja-technical-writing/ja-no-mixed-period",
                        "severity": 2,
                        "fix": {"range": [10, 11], "text": "。"},
                    },
                ],
            }
        ]
    )
    errors = pyfltr.error_parser.parse_errors("textlint", output)
    assert len(errors) == 1
    assert errors[0].rule == "ja-technical-writing/ja-no-mixed-period"
    assert errors[0].severity == "error"
    assert errors[0].fix == "safe"


def test_parse_typos_jsonl() -> None:
    """typos --format=json 出力（JSON Lines）のパース。"""
    output = (
        '{"path":"src/foo.py","line_num":3,"byte_offset":15,"typo":"teh","corrections":["the"],"type":"typo"}\n'
        '{"path":"src/bar.py","line_num":7,"byte_offset":20,"typo":"hte","corrections":["the","he"],"type":"typo"}\n'
    )
    errors = pyfltr.error_parser.parse_errors("typos", output)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 3
    assert errors[0].message == "`teh` -> `the`"
    assert errors[0].severity == "warning"
    assert errors[0].fix == "safe"
    assert errors[1].message == "`hte` -> `the, he`"


def test_parse_typos_jsonl_fallback() -> None:
    """typos: JSON Lines でない出力は regex にフォールバックする。"""
    output = "src/foo.py:3:15: `teh` -> `the`"
    errors = pyfltr.error_parser.parse_errors("typos", output)
    assert len(errors) == 1
    assert errors[0].line == 3


def test_parse_pytest_tb_line() -> None:
    """pytest --tb=line 出力からの行番号取得。"""
    output = (
        "============================= test session starts ==============================\n"
        "collected 3 items\n"
        "\n"
        "tests/foo_test.py F..                                                    [100%]\n"
        "\n"
        "================================= FAILURES =================================\n"
        "/abs/path/tests/foo_test.py:42: assert 1 == 2\n"
        "========================= short test summary info ==========================\n"
        "FAILED tests/foo_test.py::test_bar - assert 1 == 2\n"
        "========================= 1 failed, 2 passed in 0.5s =========================\n"
    )
    errors = pyfltr.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].line == 42
    assert "assert 1 == 2" in errors[0].message


def test_parse_pytest_fallback() -> None:
    """pytest: --tb=line 形式がなければ FAILED 行にフォールバック（line=0）。"""
    output = (
        "FAILED tests/foo_test.py::test_bar - AssertionError: xxx\n"
        "========================= 1 failed in 0.5s =========================\n"
    )
    errors = pyfltr.error_parser.parse_errors("pytest", output)
    assert len(errors) == 1
    assert errors[0].file == "tests/foo_test.py"
    assert errors[0].line == 0


def test_get_custom_parser_commands() -> None:
    """カスタムパーサー登録コマンド一覧の取得。"""
    commands = pyfltr.error_parser.get_custom_parser_commands()
    assert "eslint" in commands
    assert "ruff-check" in commands
    assert "pytest" in commands
    assert "mypy" not in commands
