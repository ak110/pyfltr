from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_types import CommandInfo, FunctionRef

COMMANDS: dict[str, CommandInfo] = {
    "sqlfluff": CommandInfo(
        type="linter",
        targets="*.sql",
        fixed_cost=1.0,
        per_file_cost=0.05,
        python_bin="sqlfluff",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_sqlfluff_json"),
        defaults={
            "sqlfluff": False,
            "sqlfluff-path": "",
            "sqlfluff-runner": "uvx",
            "sqlfluff-args": ["lint", "--format=json"],
            "sqlfluff-fast": False,
        },
    ),
}
