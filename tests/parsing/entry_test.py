import pathlib

import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.paths
import pyfltr.tools


@pytest.mark.parametrize("exists", [True, False])
def test_context_parser_checks_file_from_explicit_base(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, exists: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if exists:
        (project / "my scripttool").write_text("raise ValueError('sample')\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    output = (
        "===== FAILURES =====\n_____ test_sample _____\nsample_test.py:8: in test_sample\n"
        "my scripttool:2: in <module>\nE   ValueError: sample\n===== short test summary info =====\n"
        "FAILED sample_test.py::test_sample - ValueError: sample\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("pytest", output, path_base=project, start_cwd=project)
    assert [(error.file, error.line) for error in errors] == [("sample_test.py", 8)]


def test_sort_errors() -> None:
    command_names = ["ruff-check", "mypy", "pylint"]
    errors = [
        pyfltr.diagnostics.ErrorLocation(file="src/bar.py", line=10, col=None, command="mypy", message="err1"),
        pyfltr.diagnostics.ErrorLocation(file="src/bar.py", line=10, col=None, command="ruff-check", message="err2"),
        pyfltr.diagnostics.ErrorLocation(file="src/foo.py", line=5, col=None, command="mypy", message="err3"),
    ]
    sorted_errors = pyfltr.parsing.entry.sort_errors(errors, command_names)

    assert sorted_errors[0].file == "src/bar.py"
    assert sorted_errors[0].command == "ruff-check"
    assert sorted_errors[1].file == "src/bar.py"
    assert sorted_errors[1].command == "mypy"
    assert sorted_errors[2].file == "src/foo.py"


def test_parse_errors_normalizes_absolute_path() -> None:
    cwd = str(pathlib.Path.cwd())
    output = f"  {cwd}/src/foo.py:10:5 - error: some type error"
    errors = pyfltr.parsing.entry.parse_errors("pyright", output)
    assert len(errors) == 1
    assert errors[0].file == "src/foo.py"


def test_get_custom_parser_commands() -> None:
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
    assert "arid" in commands
    assert "mypy" not in commands
    assert not (
        {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.parser is not None}
        & {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.path_resolving_parser is not None}
    )
    context_commands = {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.context_parser is not None}
    assert not context_commands & {
        name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.parser or info.path_resolving_parser
    }


def test_parse_errors_error_pattern_precedes_path_resolving_parser() -> None:
    output = "src/a.py:12: 重複を検出しました"

    errors = pyfltr.parsing.entry.parse_errors("arid", output, r"(?P<file>[^:]+):(?P<line>\d+): (?P<message>.+)")

    assert [(error.file, error.line, error.message) for error in errors] == [("src/a.py", 12, "重複を検出しました")]


def test_parse_summary_mypy_via_fallback() -> None:
    output = "Success: no issues found in 42 source files\n"
    result = pyfltr.parsing.entry.parse_summary("mypy", output)
    assert result == "Success: no issues found in 42 source files"


def test_parse_summary_json_output_returns_none() -> None:
    assert pyfltr.parsing.entry.parse_summary("ruff-check", "[]") is None
    assert pyfltr.parsing.entry.parse_summary("shellcheck", "[]") is None


def test_parse_summary_empty_output() -> None:
    assert pyfltr.parsing.entry.parse_summary("mypy", "") is None
    assert pyfltr.parsing.entry.parse_summary("mypy", "  \n  ") is None


def test_parse_errors_rebases_subproject_relative_output(tmp_path: pathlib.Path) -> None:
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
    errors = pyfltr.parsing.entry.parse_errors(
        "mypy", "../outside.py:1: error: 型が違う  [assignment]", path_base=tmp_path, start_cwd=tmp_path
    )

    assert [error.file for error in errors] == [pyfltr.paths.normalize_separators(tmp_path.parent / "outside.py")]
