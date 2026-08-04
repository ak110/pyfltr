"""github_annotationsのテストコード。

新設計では`build_workflow_command`がErrorLocation 1件をGAワークフローコマンド
1行へ整形する。1行のメッセージ本体に`file:line[:col]: [tool[:rule]] msg`を
前置することで、GitHubログビューアがプロパティを除去しても生ログでfile / line /
ruleが判読できる契約とする。
"""

import json

import pyfltr.command.error_parser
import pyfltr.output.github_annotations
from tests.conftest import make_error_location as _make_error


def test_build_workflow_command_severity_mapping() -> None:
    """severity 3値が`::error` / `::warning` / `::notice`にマップされる。"""
    for severity, kind in (("error", "::error"), ("warning", "::warning"), ("info", "::notice")):
        error = _make_error("tool", "a.py", 1, "msg")
        error.severity = severity
        line = pyfltr.output.github_annotations.build_workflow_command(error)
        assert line.startswith(f"{kind} "), f"severity={severity} の整形が {kind} で始まっていない"


def test_build_workflow_command_contains_plain_prefix() -> None:
    """メッセージ本体に`file:line:col: [tool:rule] msg`が前置される。"""
    error = _make_error("ruff-check", "src/foo.py", 10, "unused", col=5)
    error.severity = "error"
    error.rule = "F401"
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert line.startswith("::error ")
    assert "file=src/foo.py" in line
    assert "line=10" in line
    assert "col=5" in line
    assert "title=ruff-check%3A F401" in line
    # メッセージ本体（`::` 以降）に plain プレフィックスが入る
    assert "::src/foo.py:10:5: [ruff-check:F401] unused" in line


def test_build_workflow_command_without_rule() -> None:
    """ruleが無い場合は`[tool]`のみでtitleもtool名のみ。"""
    error = _make_error("mypy", "src/foo.py", 3, "bad")
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "title=mypy" in line
    assert "[mypy]" in line
    # title に `%3A`（rule 区切り）が入らない
    assert "title=mypy%3A" not in line


def test_build_workflow_command_message_escaping() -> None:
    """メッセージ本体の`%`/改行はパーセントエンコードされる。"""
    error = _make_error("tool", "a.py", 1, "100%\nline2")
    error.severity = "warning"
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    # プレフィックス含め `%` は `%25`、`\n` は `%0A` にエンコードされる
    assert "100%25%0Aline2" in line


def test_build_workflow_command_no_severity_fallback_warning() -> None:
    """severity未設定は`::warning`にフォールバックする。"""
    error = _make_error("tool", "a.py", 1, "x")
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert line.startswith("::warning ")


def test_build_workflow_command_col_optional() -> None:
    """colが無い場合はプロパティとプレフィックス双方から省略される。"""
    error = _make_error("tool", "a.py", 3, "msg")
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "col=" not in line
    # プレフィックスも `a.py:3:` （col 無し）
    assert "::a.py:3: [tool] msg" in line


def test_build_workflow_command_uses_normalized_parser_columns() -> None:
    """bandit・pylintのプロパティとプレフィックスは1起点へ補正された列を用いる。"""
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
        errors = pyfltr.command.error_parser.parse_errors(command, json.dumps(raw_output))
        assert len(errors) == 1
        assert errors[0].col == 1

        line = pyfltr.output.github_annotations.build_workflow_command(errors[0])

        assert "col=1" in line
        assert "col=0" not in line
        assert expected_prefix in line


def test_build_workflow_command_includes_end_position() -> None:
    """終了位置を持つ診断は`endLine`・`endColumn`を出力する。"""
    error = _make_error("eslint", "src/a.ts", 3, "msg", col=5)
    error.end_line = 4
    error.end_col = 9
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "line=3" in line
    assert "endLine=4" in line
    assert "col=5" in line
    assert "endColumn=9" in line


def test_build_workflow_command_omits_missing_end_position() -> None:
    """終了位置を持たない診断は`endLine`・`endColumn`を出力しない。"""
    error = _make_error("mypy", "src/a.py", 3, "msg", col=5)
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "endLine=" not in line
    assert "endColumn=" not in line


def test_build_workflow_command_end_line_without_end_col() -> None:
    """終了行だけを持つ診断は`endLine`のみを出力する。"""
    error = _make_error("textlint-like", "docs/a.md", 3, "msg", col=5)
    error.end_line = 5
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "endLine=5" in line
    assert "endColumn=" not in line


def test_build_workflow_command_omits_non_positive_columns() -> None:
    """1未満の列は開始列・終了列とも出力しない。"""
    error = _make_error("bandit", "src/a.py", 3, "msg", col=0)
    error.end_line = 3
    error.end_col = 0
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "col=" not in line
    assert "endColumn=" not in line
    assert "endLine=3" in line


def test_build_workflow_command_omits_textlint_columns() -> None:
    """textlintの列は行内位置を保証できないため開始列・終了列とも省略する。"""
    error = _make_error("textlint", "docs/a.md", 3, "msg", col=12)
    error.end_line = 3
    error.end_col = 20
    line = pyfltr.output.github_annotations.build_workflow_command(error)
    assert "col=" not in line
    assert "endColumn=" not in line
    assert "endLine=3" in line
