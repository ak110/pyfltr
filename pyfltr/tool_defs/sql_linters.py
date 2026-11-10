"""sql_lintersのツール登録。"""

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_types import CommandInfo, FunctionRef

COMMANDS: dict[str, CommandInfo] = {
    # sqlfluff: Python製SQL専用linter。dialect指定が必須のため既定で無効（opt-in）。
    # 利用者が`.sqlfluff`を配置する前提とする。`sqlfluff lint`サブコマンドをlinterとして起動する
    # （`sqlfluff format`サブコマンドは対象外）。
    "sqlfluff": CommandInfo(
        type="linter",
        targets="*.sql",
        fixed_cost=1.0,
        per_file_cost=0.05,
        python_bin="sqlfluff",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_sqlfluff_json"),
        defaults={
            # sqlfluff: SQL専用linter。dialect指定が必須のため利用者の`.sqlfluff`配置を前提とするopt-in。
            # `click`へ上限を課し他ツールの更新を妨げるため本体依存から外した。
            # `uvx`既定により別環境で解決し、pyfltrの依存グラフから切り離す。
            # 利用者が版を固定したい場合は`sqlfluff-path`または`sqlfluff-runner`で上書きする。
            # `sqlfluff lint`サブコマンドをlinterとして起動する（`sqlfluff format`サブコマンドは対象外）。
            "sqlfluff": False,
            "sqlfluff-path": "",
            "sqlfluff-runner": "uvx",
            "sqlfluff-args": ["lint", "--format=json"],
            "sqlfluff-fast": False,
        },
    ),
}
