import json

import pyfltr.output.github_annotations
import pyfltr.parsing.entry
from tests.conftest import make_error_location as _make_error


def test_build_workflow_command_severity_mapping() -> None:
    for severity, kind in (("error", "::error"), ("warning", "::warning"), ("info", "::notice")):
        error = _make_error("tool", "a.py", 1, "msg")
        error.severity = severity
        line = pyfltr.output.github_annotations.build_workflow_command(error)
        assert line.startswith(f"{kind} "), f"severity={severity} の整形が {kind} で始まっていない"


def test_build_workflow_command_contains_plain_prefix() -> None:
    error = _make_error("ruff-check", "src/foo.py", 10, "unused", col=5)
    error.severity = "error"
    error.rule = "F401"
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert line.startswith("::error ")
    assert "file=src/foo.py" in line
    assert "line=10" in line
    assert "col=5" in line
    assert "title=ruff-check%3A F401" in line
    assert "::src/foo.py:10:5: [ruff-check:F401] unused" in line


def test_build_workflow_command_without_rule() -> None:
    error = _make_error("mypy", "src/foo.py", 3, "bad")
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "title=mypy" in line
    assert "[mypy]" in line
    assert "title=mypy%3A" not in line


def test_build_workflow_command_message_escaping() -> None:
    error = _make_error("tool", "a.py", 1, "100%\nline2")
    error.severity = "warning"
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "100%25%0Aline2" in line


def test_build_workflow_command_no_severity_fallback_warning() -> None:
    error = _make_error("tool", "a.py", 1, "x")
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert line.startswith("::warning ")


def test_build_workflow_command_col_optional() -> None:
    error = _make_error("tool", "a.py", 3, "msg")
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "col=" not in line
    assert "::a.py:3: [tool] msg" in line


def test_build_workflow_command_uses_normalized_parser_columns() -> None:
    cases = [
        (
            "bandit",
            {
                "results": [
                    {
                        "filename": "src/foo.py",
                        "line_number": 12,
                        "col_offset": 0,
                        "test_id": "B101",
                        "issue_text": "Use of assert detected.",
                    }
                ]
            },
            "::src/foo.py:12:1: [bandit:B101] Use of assert detected.",
        ),
        (
            "pylint",
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
                    }
                ]
            },
            "::src/foo.py:1:1: [pylint:missing-module-docstring] C0114: Missing module docstring",
        ),
    ]

    for command, raw_output, expected_prefix in cases:
        errors = pyfltr.parsing.entry.parse_errors(command, json.dumps(raw_output))
        assert len(errors) == 1
        assert errors[0].col == 1

        line = pyfltr.output.github_annotations.build_workflow_command(errors[0])

        assert "col=1" in line
        assert "col=0" not in line
        assert expected_prefix in line


def test_build_workflow_command_includes_end_position() -> None:
    error = _make_error("eslint", "src/a.ts", 3, "msg", col=5)
    error.end_line = 4
    error.end_col = 9
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "line=3" in line
    assert "endLine=4" in line
    assert "col=5" in line
    assert "endColumn=9" in line


def test_build_workflow_command_omits_missing_end_position() -> None:
    error = _make_error("mypy", "src/a.py", 3, "msg", col=5)
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "endLine=" not in line
    assert "endColumn=" not in line


def test_build_workflow_command_end_line_without_end_col() -> None:
    error = _make_error("textlint-like", "docs/a.md", 3, "msg", col=5)
    error.end_line = 5
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "endLine=5" in line
    assert "endColumn=" not in line


def test_build_workflow_command_omits_non_positive_columns() -> None:
    error = _make_error("bandit", "src/a.py", 3, "msg", col=0)
    error.end_line = 3
    error.end_col = 0
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "col=" not in line
    assert "endColumn=" not in line
    assert "endLine=3" in line


def test_build_workflow_command_omits_textlint_columns() -> None:
    error = _make_error("textlint", "docs/a.md", 3, "msg", col=12)
    error.end_line = 3
    error.end_col = 20
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "col=" not in line
    assert "endColumn=" not in line
    assert "endLine=3" in line
