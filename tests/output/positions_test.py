import pyfltr.output.positions
from tests.conftest import make_error_location as _make_error


def test_is_publishable_column_rejects_missing_column() -> None:
    error = _make_error("mypy", "src/a.py", 3, "msg")
    assert not pyfltr.output.positions.is_publishable_column(error, error.col)


def test_is_publishable_column_rejects_non_positive_column() -> None:
    error = _make_error("bandit", "src/a.py", 3, "msg", col=0)
    assert not pyfltr.output.positions.is_publishable_column(error, error.col)


def test_is_publishable_column_rejects_textlint() -> None:
    error = _make_error("textlint", "docs/a.md", 3, "msg", col=12)
    assert not pyfltr.output.positions.is_publishable_column(error, error.col)
