import pytest

import pyfltr.command.structured_output
import pyfltr.tool_types
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
    spec = pyfltr.tool_types.StructuredOutputSpec(inject=["--message-format=short"], conflicts=[], before_argument=before)
    assert pyfltr.command.structured_output.apply_structured_output(command, spec) == expected
