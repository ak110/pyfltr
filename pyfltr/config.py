"""設定関連の処理。"""

import dataclasses
import pathlib
import tomllib
import typing

CONFIG: dict[str, typing.Any] = {
    # プリセット
    "preset": "",
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
    "pyright": False,
    "pyright-path": "pyright",
    "pyright-args": [],
    "pytest": True,
    "pytest-path": "pytest",
    "pytest-args": [],
    "pytest-devmode": True,  # PYTHONDEVMODE=1をするか否か
    "ruff-format": False,
    "ruff-format-path": "ruff",
    "ruff-format-args": ["format", "--exit-non-zero-on-format"],
    "ruff-check": False,
    "ruff-check-path": "ruff",
    "ruff-check-args": ["check"],
    # flake8風無視パターン。
    "exclude": [
        # ここの値はflake8やblackなどの既定値を元に適当に。
        # https://github.com/github/gitignore/blob/master/Python.gitignore
        # https://github.com/github/gitignore/blob/main/Node.gitignore
        "*.egg",
        "*.egg-info",
        ".bzr",
        ".cache",
        ".claude",
        ".clinerules",
        ".direnv",
        ".eggs",
        ".git",
        ".github",
        ".hg",
        ".mypy_cache",
        ".nox",
        ".pnpm",
        ".pyre",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        ".vite",
        ".vscode",
        ".yarn",
        "CVS",
        "__pycache__",
        "__pypackages__",
        "_build",
        "buck-out",
        "build",
        "dist",
        "node_modules",
        "venv",
    ],
    "extend-exclude": [],
    # コマンド名のエイリアス
    "aliases": {
        "format": ["pyupgrade", "autoflake", "isort", "black", "ruff-format"],
        "lint": ["ruff-check", "pflake8", "mypy", "pylint", "pyright"],
        "test": ["pytest"],
        "fast": ["pyupgrade", "autoflake", "isort", "black", "ruff-format", "ruff-check", "pflake8"],
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
    "ruff-check": CommandInfo(type="linter"),
    "pflake8": CommandInfo(type="linter"),
    "mypy": CommandInfo(type="linter"),
    "pylint": CommandInfo(type="linter"),
    "pyright": CommandInfo(type="linter"),
    "pytest": CommandInfo(type="tester"),
}
"""全コマンドの情報。"""

ALL_COMMAND_NAMES: list[str] = list(ALL_COMMANDS.keys())
"""全コマンドの名前のリスト。

タブなどの並び順はこのリストの順に従うことにする。
"""


def load_config() -> None:
    """pyproject.tomlから設定を読み込み。"""
    pyproject_path = pathlib.Path("pyproject.toml").absolute()
    if not pyproject_path.exists():
        return

    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    tool_pyfltr = pyproject_data.get("tool", {}).get("pyfltr", {})

    # プリセットの反映 (CONFIGに直接)
    preset = str(tool_pyfltr.get("preset", ""))
    if preset == "":
        pass
    elif preset in ("20250710", "latest"):
        # ruff使用のプリセット
        CONFIG["pyupgrade"] = False
        CONFIG["autoflake"] = False
        CONFIG["pflake8"] = False
        CONFIG["isort"] = False
        CONFIG["black"] = False
        CONFIG["ruff-format"] = True
        CONFIG["ruff-check"] = True
    else:
        raise ValueError(f"presetの設定値が不正です。{preset=}")

    # プリセット以外の設定を適用 (プリセットと重複があれば上書き)
    for key, value in tool_pyfltr.items():
        key = key.replace("_", "-")  # 「_」区切りと「-」区切りのどちらもOK
        if key not in CONFIG:
            raise ValueError(f"Invalid config key: {key}")
        if not isinstance(value, type(CONFIG[key])):  # 簡易チェック
            raise ValueError(f"invalid config value: {key}={type(value)}, expected {type(CONFIG[key])}")
        CONFIG[key] = value


def resolve_aliases(commands: list[str]) -> list[str]:
    """エイリアスを展開。"""
    # 最大10回まで再帰的に展開
    result: list[str] = []
    for _ in range(10):
        result = []
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
    result.sort(key=ALL_COMMAND_NAMES.index)  # リスト順にソート
    return result


def generate_config_text() -> str:
    """設定ファイルのサンプルテキストを生成。"""
    return "[tool.pyfltr]\n" + "\n".join(
        f"{key} = " + repr(value).replace("'", '"').replace("True", "true").replace("False", "false")
        for key, value in CONFIG.items()
    )
