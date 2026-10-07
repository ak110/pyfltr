import pathlib

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools


def test_sort_errors() -> None:
    """エラーソートのテスト。"""
    command_names = ["ruff-check", "mypy", "pylint"]
    errors = [
        pyfltr.diagnostics.ErrorLocation(file="src/bar.py", line=10, col=None, command="mypy", message="err1"),
        pyfltr.diagnostics.ErrorLocation(file="src/bar.py", line=10, col=None, command="ruff-check", message="err2"),
        pyfltr.diagnostics.ErrorLocation(file="src/foo.py", line=5, col=None, command="mypy", message="err3"),
    ]
    sorted_errors = pyfltr.parsing.entry.sort_errors(errors, command_names)

    # ファイル名でソート→同一箇所はcommand_names順
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
    errors = pyfltr.parsing.entry.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"  # 相対パスになっている


def test_get_custom_parser_commands() -> None:
    """カスタムパーサー登録コマンド一覧の取得。"""
    commands = pyfltr.parsing.entry.get_custom_parser_commands()
    assert "eslint" in commands
    assert "ruff-check" in commands
    assert "pytest" in commands
    assert "designmd" in commands
    assert "lychee" in commands
    assert "uv-audit" in commands
    assert "npm-audit" in commands
    assert "pnpm-audit" in commands
    assert "yarn-audit" in commands
    assert "semgrep" in commands
    assert "sqlfluff" in commands
    assert "pinact" in commands
    # aridは`_PATH_BASE_PARSERS`側の登録だが、UIのストリーミング抑止判定では同じ集合に含める。
    assert "arid" in commands
    assert "mypy" not in commands
    # 2つのパーサー表は`parse_errors`が順に引くため、同じコマンドを両方へ登録しない。
    # 表の排他性は公開関数の戻り値へ現れないため直接検証する。
    assert not (
        {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.parser is not None}
        & {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.path_base_parser is not None}
    )


def test_parse_errors_error_pattern_precedes_path_base_parser() -> None:
    """`error_pattern`の指定は`path_base`を要するパーサーより優先する。"""
    output = "src/a.py:12: 重複を検出しました"

    errors = pyfltr.parsing.entry.parse_errors("arid", output, r"(?P<file>[^:]+):(?P<line>\d+): (?P<message>.+)")

    assert [(error.file, error.line, error.message) for error in errors] == [("src/a.py", 12, "重複を検出しました")]


def test_parse_summary_mypy_via_fallback() -> None:
    """mypy: 汎用フォールバックでSuccess行を抽出する。"""
    output = "Success: no issues found in 42 source files\n"
    result = pyfltr.parsing.entry.parse_summary("mypy", output)
    assert result == "Success: no issues found in 42 source files"


def test_parse_summary_json_output_returns_none() -> None:
    """JSON出力（[]等）は汎用フォールバックでNoneを返す。"""
    assert pyfltr.parsing.entry.parse_summary("ruff-check", "[]") is None
    assert pyfltr.parsing.entry.parse_summary("shellcheck", "[]") is None


def test_parse_summary_empty_output() -> None:
    """空出力はNoneを返す。"""
    assert pyfltr.parsing.entry.parse_summary("mypy", "") is None
    assert pyfltr.parsing.entry.parse_summary("mypy", "  \n  ") is None
