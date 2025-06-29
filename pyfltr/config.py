"""設定関連の処理。"""

import dataclasses
import pathlib
import tomllib
import typing

CONFIG: dict[str, typing.Any] = {
    # コマンド毎に有効無効、パス、追加の引数を設定
    "pyupgrade": True,
    "pyupgrade-path": "pyupgrade",
    "pyupgrade-args": [],
    "autoflake": True,
    "autoflake-path": "autoflake",
    "autoflake-args": [
        "--in-place",
        "--remove-all-unused-imports",
        "--ignore-init-module-imports",
        "--remove-unused-variables",
        "--verbose",
    ],
    "isort": True,
    "isort-path": "isort",
    "isort-args": ["--settings-path=./pyproject.toml"],
    "black": True,
    "black-path": "black",
    "black-args": [],
    "pflake8": True,
    "pflake8-path": "pflake8",
    "pflake8-args": [],
    "mypy": True,
    "mypy-path": "mypy",
    "mypy-args": [],
    "pylint": True,
    "pylint-path": "pylint",
    "pylint-args": [],
    "pytest": True,
    "pytest-path": "pytest",
    "pytest-args": [],
    "pytest-devmode": True,  # PYTHONDEVMODE=1をするか否か
    "ruff-format": False,
    "ruff-format-path": "ruff",
    "ruff-format-args": ["format", "--exit-non-zero-on-format"],
    "ruff-check": False,
    "ruff-check-path": "ruff",
    "ruff-check-args": ["check", "--fix", "--exit-non-zero-on-fix"],
    # flake8風無視パターン。
    "exclude": [
        # ここの値はflake8やblackなどの既定値を元に適当に。
        "*.egg",
        ".bzr",
        ".direnv",
        ".eggs",
        ".git",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".svn",
        ".tox",
        ".venv",
        "CVS",
        "__pycache__",
        "_build",
        "buck-out",
        "build",
        "dist",
        "venv",
    ],
    "extend-exclude": [],
    # コマンド名のエイリアス
    "aliases": {
        "format": ["pyupgrade", "autoflake", "isort", "black", "ruff-format", "ruff-check"],
        "lint": ["pflake8", "mypy", "pylint"],
        "test": ["pytest"],
        "fast": ["pyupgrade", "autoflake", "isort", "black", "pflake8", "ruff-format", "ruff-check"],
    },
}
"""デフォルト設定。"""

CommandType = typing.Literal["formatter", "linter", "tester"]
"""コマンドの種類。"""


@dataclasses.dataclass
class CommandInfo:
    """コマンドの情報を保持する辞書型。"""

    type: CommandType
    """コマンドの種類（formatter, linter, tester）"""


ALL_COMMANDS: dict[str, CommandInfo] = {
    "pyupgrade": CommandInfo(type="formatter"),
    "autoflake": CommandInfo(type="formatter"),
    "isort": CommandInfo(type="formatter"),
    "black": CommandInfo(type="formatter"),
    "ruff-format": CommandInfo(type="formatter"),
    "ruff-check": CommandInfo(type="formatter"),  # ファイル変更を伴う可能性があるためformatter扱い
    "pflake8": CommandInfo(type="linter"),
    "mypy": CommandInfo(type="linter"),
    "pylint": CommandInfo(type="linter"),
    "pytest": CommandInfo(type="tester"),
}
"""全コマンドの情報。"""


def load_config() -> None:
    """pyproject.tomlから設定を読み込み。"""
    pyproject_path = pathlib.Path("pyproject.toml").absolute()
    if not pyproject_path.exists():
        return

    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    for key, value in pyproject_data.get("tool", {}).get("pyfltr", {}).items():
        key = key.replace("_", "-")  # 「_」区切りと「-」区切りのどちらもOK
        if key not in CONFIG:
            raise ValueError(f"Invalid config key: {key}")
        if not isinstance(value, type(CONFIG[key])):  # 簡易チェック
            raise ValueError(f"invalid config value: {key}={type(value)}, expected {type(CONFIG[key])}")
        CONFIG[key] = value


def resolve_aliases(commands: list[str]) -> list[str]:
    """エイリアスを展開。"""
    # 最大10回まで再帰的に展開
    for _ in range(10):
        result: list[str] = []
        resolved: bool = False
        for command in commands:
            command = command.strip()
            if command in CONFIG["aliases"]:
                for c in CONFIG["aliases"][command]:
                    if c not in result:  # 順番は維持しつつ重複排除
                        result.append(c)
                resolved = True
            else:
                if command not in result:  # 順番は維持しつつ重複排除
                    result.append(command)
        if not resolved:
            break
        commands = result
    return result


def generate_config_text() -> str:
    """設定ファイルのサンプルテキストを生成。"""
    return "[tool.pyfltr]\n" + "\n".join(
        f"{key} = " + repr(value).replace("'", '"').replace("True", "true").replace("False", "false")
        for key, value in CONFIG.items()
    )
