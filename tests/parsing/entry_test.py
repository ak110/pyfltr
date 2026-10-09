import pathlib

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.paths
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
    # aridは`path_resolving_parser`側の登録だが、UIのストリーミング抑止判定では同じ集合に含める。
    assert "arid" in commands
    assert "mypy" not in commands
    # 2つのパーサー表は`parse_errors`が順に引くため、同じコマンドを両方へ登録しない。
    # 表の排他性は公開関数の戻り値へ現れないため直接検証する。
    assert not (
        {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.parser is not None}
        & {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.path_resolving_parser is not None}
    )


def test_parse_errors_error_pattern_precedes_path_resolving_parser() -> None:
    """`error_pattern`の指定はパス変換関数を受け取るパーサーより優先する。"""
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


def test_parse_errors_rebases_subproject_relative_output(tmp_path: pathlib.Path) -> None:
    """子プロジェクトのcwdで実行したツールの相対パス診断を起点相対へ揃える。

    組み込み正規表現・関数パーサー・利用者指定の`error_pattern`の全経路で同じ基準を使う。
    """
    sub = tmp_path / "pkg_a"
    mypy_errors = pyfltr.parsing.entry.parse_errors(
        "mypy", "x.py:1: error: 型が違う  [assignment]", path_base=sub, start_cwd=tmp_path
    )
    pytest_errors = pyfltr.parsing.entry.parse_errors(
        "pytest",
        "================================= FAILURES =================================\n"
        "_______________________________ test_a ________________________________\n"
        "tests/x_test.py:3: in test_a\n"
        "    assert value\n"
        "E   AssertionError: assert 0\n",
        path_base=sub,
        start_cwd=tmp_path,
    )
    custom_errors = pyfltr.parsing.entry.parse_errors(
        "custom", "x.py:2: 指摘", r"(?P<file>[^:]+):(?P<line>\d+): (?P<message>.+)", path_base=sub, start_cwd=tmp_path
    )

    assert [error.file for error in mypy_errors] == ["pkg_a/x.py"]
    assert [error.file for error in pytest_errors] == ["pkg_a/tests/x_test.py"]
    assert [error.file for error in custom_errors] == ["pkg_a/x.py"]


def test_parse_errors_does_not_double_prefix_absolute_or_remapped_paths(tmp_path: pathlib.Path) -> None:
    """絶対パス・起点外のパス・一時パスから元ファイルへ戻した診断へ接頭辞を重ねない。"""
    sub = tmp_path / "pkg_a"
    inside = sub / "doc.md"
    outside = tmp_path.parent / "external.md"
    temporary = tmp_path.parent / "pyfltr-tmp" / "doc.md"
    output = "\n".join(f"{path}:1:1: 指摘" for path in (inside, outside, temporary))

    errors = pyfltr.parsing.entry.parse_errors(
        "custom",
        output,
        r"(?P<file>.+):(?P<line>\d+):(?P<col>\d+): (?P<message>.+)",
        file_path_remap={str(temporary): "pkg_a/doc.md"},
        path_base=sub,
        start_cwd=tmp_path,
    )

    assert [error.file for error in errors] == [
        "pkg_a/doc.md",
        pyfltr.paths.normalize_separators(outside),
        "pkg_a/doc.md",
    ]


def test_parse_errors_returns_absolute_path_for_relative_outside_start(tmp_path: pathlib.Path) -> None:
    """単一プロジェクトでも起点外を指す`../`相対の診断は`/`区切りの絶対パスで返す。

    起点外の対象ファイルは対象集合側でも絶対パスで保持されるため、同じ表現に揃えて突合できるようにする。
    """
    errors = pyfltr.parsing.entry.parse_errors(
        "mypy", "../outside.py:1: error: 型が違う  [assignment]", path_base=tmp_path, start_cwd=tmp_path
    )

    assert [error.file for error in errors] == [pyfltr.paths.normalize_separators(tmp_path.parent / "outside.py")]
