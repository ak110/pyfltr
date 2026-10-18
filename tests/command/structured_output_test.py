"""構造化引数の注入位置を検証する。"""

import pytest

import pyfltr.command.structured_output
import pyfltr.tools


@pytest.mark.parametrize(
    ("command", "before", "expected"),
    [
        (
            ["mise", "exec", "cargo", "--", "cargo", "clippy", "--", "-D", "warnings"],
            "--",
            ["mise", "exec", "cargo", "--", "cargo", "clippy", "--message-format=short", "--", "-D", "warnings"],
        ),
        (["cargo", "deny", "check"], "check", ["cargo", "deny", "--message-format=short", "check"]),
        (["cargo", "clippy"], "--", ["cargo", "clippy", "--message-format=short"]),
    ],
)
def test_apply_structured_output_before_argument(command: list[str], before: str, expected: list[str]) -> None:
    """miseと下位コマンドの引数境界を保ち、指定したマーカーの前へ注入する。"""
    spec = pyfltr.tools.StructuredOutputSpec(inject=["--message-format=short"], conflicts=[], before_argument=before)
    assert pyfltr.command.structured_output.apply_structured_output(command, spec) == expected
