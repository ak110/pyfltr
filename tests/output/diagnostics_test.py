import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools


def test_format_error() -> None:
    """エラーフォーマットのテスト。"""
    error = pyfltr.diagnostics.ErrorLocation(file="src/foo.py", line=10, col=5, command="mypy", message="some error")
    assert pyfltr.output.diagnostics.format_error(error) == "src/foo.py:10:5: [mypy] some error"

    # colなし
    error_no_col = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py", line=10, col=None, command="ruff-check", message="another error"
    )
    assert pyfltr.output.diagnostics.format_error(error_no_col) == "src/foo.py:10: [ruff-check] another error"

    # ruleあり
    error_with_rule = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py", line=10, col=5, command="ruff-check", message="`os` imported but unused", rule="F401"
    )
    assert (
        pyfltr.output.diagnostics.format_error(error_with_rule) == "src/foo.py:10:5: [ruff-check:F401] `os` imported but unused"
    )


@pytest.mark.parametrize(
    "severity,expected_message",
    [
        ("error", "src/foo.py:10: [designmd] critical issue"),
        ("warning", "src/foo.py:10: [designmd] critical issue"),
        ("info", "src/foo.py:10: [designmd] [INFO] critical issue"),
        (None, "src/foo.py:10: [designmd] critical issue"),
    ],
)
def test_format_error_severity_info_prefix(severity: str | None, expected_message: str) -> None:
    """severity=="info"のときmessage先頭に[INFO] を付加し、他のseverityでは表記を変更しない。"""
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=None,
        command="designmd",
        message="critical issue",
        severity=severity,
    )
    assert pyfltr.output.diagnostics.format_error(error) == expected_message
