"""positions のテストコード。"""

import pyfltr.output.positions
from tests.conftest import make_error_location as _make_error


def test_is_publishable_column_accepts_in_line_column() -> None:
    """1以上の列は出力対象になる。"""
    error = _make_error("ruff-check", "src/a.py", 3, "msg", col=5)
    assert pyfltr.output.positions.is_publishable_column(error, error.col)


def test_is_publishable_column_rejects_missing_column() -> None:
    """列が無い場合は出力しない。"""
    error = _make_error("mypy", "src/a.py", 3, "msg")
    assert not pyfltr.output.positions.is_publishable_column(error, error.col)


def test_is_publishable_column_rejects_non_positive_column() -> None:
    """1未満の列は行内位置として成立しないため出力しない。"""
    error = _make_error("bandit", "src/a.py", 3, "msg", col=0)
    assert not pyfltr.output.positions.is_publishable_column(error, error.col)


def test_is_publishable_column_rejects_textlint() -> None:
    """textlintの列は行内位置を保証できないため出力しない。"""
    error = _make_error("textlint", "docs/a.md", 3, "msg", col=12)
    assert not pyfltr.output.positions.is_publishable_column(error, error.col)
