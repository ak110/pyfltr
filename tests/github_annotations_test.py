"""github_annotationsのテストコード。

新設計では ``build_workflow_command`` が ErrorLocation 1 件を GA ワークフローコマンド
1 行へ整形する。1 行のメッセージ本体に ``file:line[:col]: [tool[:rule]] msg`` を
前置することで、GitHub ログビューアがプロパティを剥がしても生ログで file / line /
rule が読める契約とする。
"""

import pyfltr.github_annotations
from tests.conftest import make_error_location as _make_error


def test_build_workflow_command_severity_mapping() -> None:
    """severity 3 値が `::error` / `::warning` / `::notice` にマップされる。"""
    for severity, kind in (("error", "::error"), ("warning", "::warning"), ("info", "::notice")):
        error = _make_error("tool", "a.py", 1, "msg")
        error.severity = severity
        line = pyfltr.github_annotations.build_workflow_command(error)
        assert line.startswith(f"{kind} "), f"severity={severity} の整形が {kind} で始まっていない"


def test_build_workflow_command_contains_plain_prefix() -> None:
    """メッセージ本体に ``file:line:col: [tool:rule] msg`` が前置される。"""
    error = _make_error("ruff-check", "src/foo.py", 10, "unused", col=5)
    error.severity = "error"
    error.rule = "F401"
    line = pyfltr.github_annotations.build_workflow_command(error)
    assert line.startswith("::error ")
    assert "file=src/foo.py" in line
    assert "line=10" in line
    assert "col=5" in line
    assert "title=ruff-check%3A F401" in line
    # メッセージ本体（`::` 以降）に plain プレフィックスが入る
    assert "::src/foo.py:10:5: [ruff-check:F401] unused" in line


def test_build_workflow_command_without_rule() -> None:
    """rule が無い場合は ``[tool]`` のみで title も tool 名のみ。"""
    error = _make_error("mypy", "src/foo.py", 3, "bad")
    line = pyfltr.github_annotations.build_workflow_command(error)
    assert "title=mypy" in line
    assert "[mypy]" in line
    # title に `%3A`（rule 区切り）が入らない
    assert "title=mypy%3A" not in line


def test_build_workflow_command_message_escaping() -> None:
    """メッセージ本体の ``%`` / 改行はパーセントエンコードされる。"""
    error = _make_error("tool", "a.py", 1, "100%\nline2")
    error.severity = "warning"
    line = pyfltr.github_annotations.build_workflow_command(error)
    # プレフィックス含め `%` は `%25`、`\n` は `%0A` にエンコードされる
    assert "100%25%0Aline2" in line


def test_build_workflow_command_no_severity_fallback_warning() -> None:
    """severity 未設定は ``::warning`` にフォールバックする。"""
    error = _make_error("tool", "a.py", 1, "x")
    line = pyfltr.github_annotations.build_workflow_command(error)
    assert line.startswith("::warning ")


def test_build_workflow_command_col_optional() -> None:
    """col が無い場合はプロパティとプレフィックス双方から省略される。"""
    error = _make_error("tool", "a.py", 3, "msg")
    line = pyfltr.github_annotations.build_workflow_command(error)
    assert "col=" not in line
    # プレフィックスも `a.py:3:` （col 無し）
    assert "::a.py:3: [tool] msg" in line
