"""python_lintersのツール登録。"""

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_types import CommandInfo, FunctionRef, StructuredOutputSpec

COMMANDS: dict[str, CommandInfo] = {
    # Python linter群はモダン順（後ろほど新しい）に並べる。実行順はLPT並列で別管理。
    "pylint": CommandInfo(
        type="linter",
        fixed_cost=1.75,
        per_file_cost=0.3,
        auto_args=[
            ("pylint-pydantic", ["--load-plugins=pylint_pydantic"]),
        ],
        python_bin="pylint",
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[CRWEF]\d+:.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_pylint_json"),
        summary_parser=FunctionRef[[str], str | None]("pyfltr.parsing.tools", "summarize_pylint_json"),
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_pylint_url"),
        structured_output=(
            "pylint-json",
            StructuredOutputSpec(
                inject=["--output-format=json2"],
                conflicts=["--output-format"],
            ),
        ),
        language="python",
        defaults={
            # 自動オプション: 各ツールの望ましい引数を自動挿入する。
            # *-argsとは独立して動作し、重複排除される。Falseで無効化可能。
            "pylint-pydantic": True,
            "pylint-json": True,
            "pylint": False,
            "pylint-path": "",
            "pylint-args": [],
            "pylint-runner": "python-runner",
            "pylint-fast": False,
        },
    ),
    "mypy": CommandInfo(
        type="linter",
        fixed_cost=0.2,
        per_file_cost=0.12,
        auto_args=[
            ("mypy-unused-awaitable", ["--enable-error-code=unused-awaitable"]),
        ],
        python_bin="mypy",
        subproject_concurrent=False,
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):\s*error:\s*(?P<message>.+?)(?:\s*\[(?P<rule>[^\]]+)\])?\s*$",
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_mypy_url"),
        language="python",
        defaults={
            "mypy-unused-awaitable": True,
            # コマンド毎に有効無効、パス、追加の引数を設定
            # 言語カテゴリ（python / javascript / rust / dotnet）に属するツールはv3.0.0で
            # opt-in化したため、既定値はFalse。presetで推奨ツールがTrueになり、
            # カテゴリキー（`python = true`等）がgateを開けて有効化を通す構造。
            # presetを使わず個別に`{command} = true`を指定するとgateを越えて最優先で有効化される。
            "mypy": False,
            # pathが空文字の場合は{command}-runner設定
            # （既定はツール群に応じてpython-runner/js-runner/bin-runner）に基づいて自動解決する。
            # python-runner経路の既定（"uv"）によりcwdのuv.lock検出時はプロジェクトのuv環境を使う。
            # {command}-path明示で従来挙動（指定パスを直接実行）に切り替えられる。
            "mypy-path": "",
            "mypy-args": [],
            "mypy-runner": "python-runner",
            "mypy-fast": False,
        },
    ),
    "ruff-check": CommandInfo(
        type="linter",
        fixed_cost=0.01,
        python_bin="ruff",
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[A-Z]+\d+\s+.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_ruff_check_json"),
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_ruff_url"),
        structured_output=(
            "ruff-check-json",
            StructuredOutputSpec(
                inject=["--output-format=json"],
                conflicts=["--output-format"],
            ),
        ),
        language="python",
        defaults={
            # 構造化出力: 対応ツールの出力形式をJSON等に切り替え、パーサーで
            # ルールコード・severity・fix情報を構造化して取得する。
            # *-argsとは独立した経路で注入されるためpyproject.tomlの上書きに影響されない。
            "ruff-check-json": True,
            "ruff-check": False,
            "ruff-check-path": "",
            "ruff-check-runner": "python-runner",
            "ruff-check-args": ["check"],
            "ruff-check-fast": True,
            # fixモード時に通常argsの後に追加する引数。
            # `ruff check --fix --unsafe-fixes`でautofix可能な違反を修正する。
            # unsafe fix採用方針はbiome側（`biome-fix-args`）と統一する。
            # （通常モードのruff-format-by-checkとは別経路で動作する）
            "ruff-check-fix-args": ["--fix", "--unsafe-fixes"],
        },
    ),
    "pyright": CommandInfo(
        type="linter",
        fixed_cost=0.8,
        per_file_cost=0.155,
        python_bin="pyright",
        diagnostic_pattern=r"^\s*(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)\s*-\s*error:\s*(?P<message>.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_pyright_json"),
        summary_parser=FunctionRef[[str], str | None]("pyfltr.parsing.tools", "summarize_pyright_json"),
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_pyright_url"),
        structured_output=(
            "pyright-json",
            StructuredOutputSpec(
                inject=["--outputjson"],
                conflicts=["--outputjson"],
            ),
        ),
        language="python",
        defaults={
            "pyright-json": True,
            "pyright": False,
            "pyright-path": "",
            "pyright-args": [],
            "pyright-runner": "python-runner",
            "pyright-fast": False,
        },
    ),
    "ty": CommandInfo(
        type="linter",
        fixed_cost=0.05,
        per_file_cost=0.01,
        python_bin="ty",
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):"
        r"\s*(?P<severity>error|warning|info)\[(?P<rule>[^\]]+)\]\s+(?P<message>.+)",
        language="python",
        defaults={
            "ty": False,
            "ty-path": "",
            "ty-args": ["check", "--output-format", "concise", "--error-on-warning"],
            "ty-runner": "python-runner",
            "ty-fast": True,
        },
    ),
    "arid": CommandInfo(
        type="linter",
        fixed_cost=0.05,
        per_file_cost=0.0002,
        python_bin="arid",
        path_resolving_parser=FunctionRef[..., list[ErrorLocation]]("pyfltr.parsing.tools", "parse_arid_json"),
        structured_output=(
            "arid-json",
            StructuredOutputSpec(
                inject=["--format=json"],
                conflicts=["--format", "--json"],
            ),
        ),
        language="python",
        defaults={
            "arid-json": True,
            "arid": False,
            "arid-path": "",
            "arid-args": ["--project-root", "."],
            "arid-runner": "python-runner",
            "arid-fast": True,
        },
    ),
}
