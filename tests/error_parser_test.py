"""error_parserのテストコード。"""

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
        # pytest
        (
            "pytest",
            "FAILED tests/foo_test.py::test_bar - AssertionError: xxx",
            1,
            "tests/foo_test.py",
            0,  # pytestはline情報なし
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
