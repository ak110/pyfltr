"""設定関連の処理。"""

import copy
import dataclasses
import pathlib
import re
import tomllib
import typing

CommandType = typing.Literal["formatter", "linter", "tester"]
"""コマンドの種類。"""


@dataclasses.dataclass
class CommandInfo:
    """コマンドの情報。"""

    type: CommandType
    """コマンドの種類（formatter, linter, tester）"""
    builtin: bool = True
    """ビルトインコマンドか否か"""
    targets: str = "*.py"
    """対象ファイルパターン"""
    error_pattern: str | None = None
    """エラーパース用正規表現"""


# ビルトインコマンド定義（順序が並び順を決める）
BUILTIN_COMMANDS: dict[str, CommandInfo] = {
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
    "ty": CommandInfo(type="linter"),
    "markdownlint": CommandInfo(type="linter", targets="*.md"),
    "textlint": CommandInfo(type="linter", targets="*.md"),
    "pytest": CommandInfo(type="tester", targets="*_test.py"),
}

BUILTIN_COMMAND_NAMES: list[str] = list(BUILTIN_COMMANDS.keys())
"""ビルトインコマンドの名前リスト。"""


DEFAULT_CONFIG: dict[str, typing.Any] = {
    # プリセット
    "preset": "",
    # コマンド毎に有効無効、パス、追加の引数を設定
    "pyupgrade": True,
    "pyupgrade-path": "pyupgrade",
    "pyupgrade-args": [],
    "pyupgrade-fast": True,
    "autoflake": True,
    "autoflake-path": "autoflake",
    "autoflake-args": [
        "--in-place",
        "--remove-all-unused-imports",
        "--ignore-init-module-imports",
        "--remove-unused-variables",
        "--verbose",
    ],
    "autoflake-fast": True,
    "isort": True,
    "isort-path": "isort",
    "isort-args": ["--settings-path=./pyproject.toml"],
    "isort-fast": True,
    "black": True,
    "black-path": "black",
    "black-args": [],
    "black-fast": True,
    "pflake8": True,
    "pflake8-path": "pflake8",
    "pflake8-args": [],
    "pflake8-fast": True,
    "mypy": True,
    "mypy-path": "mypy",
    "mypy-args": [],
    "mypy-fast": False,
    "pylint": True,
    "pylint-path": "pylint",
    "pylint-args": [],
    "pylint-fast": False,
    "pyright": False,
    "pyright-path": "pyright",
    "pyright-args": [],
    "pyright-fast": False,
    "ty": False,
    "ty-path": "ty",
    "ty-args": ["check", "--output-format", "concise", "--error-on-warning"],
    "ty-fast": True,
    "markdownlint": False,
    "markdownlint-path": "pnpx",
    "markdownlint-args": ["markdownlint-cli2"],
    "markdownlint-fast": True,
    "textlint": False,
    "textlint-path": "pnpx",
    "textlint-args": [
        "--package",
        "textlint",
        "--package",
        "textlint-rule-preset-ja-technical-writing",
        "textlint",
        "--format",
        "compact",
    ],
    "textlint-fast": True,
    "pytest": True,
    "pytest-path": "pytest",
    "pytest-args": [],
    "pytest-devmode": True,  # PYTHONDEVMODE=1をするか否か
    "pytest-fast": False,
    "ruff-format": False,
    "ruff-format-path": "ruff",
    "ruff-format-args": ["format", "--exit-non-zero-on-format"],
    "ruff-format-fast": True,
    # ruff-format 実行時に ruff check --fix --unsafe-fixes を先に実行するか。
    # 既定では有効とし、未整形の import ソートや安全に自動修正できる lint 違反を
    # フォーマットと一緒に片付ける (ruff 公式推奨ワークフローの発展形)。
    # lint エラーは別途 ruff-check で検出される前提のため、ステップ 1 の
    # lint violation (exit 1) は ruff-format 側では失敗扱いしない。
    "ruff-format-by-check": True,
    "ruff-format-check-args": ["check", "--fix", "--unsafe-fixes"],
    "ruff-check": False,
    "ruff-check-path": "ruff",
    "ruff-check-args": ["check"],
    "ruff-check-fast": True,
    # 最大並列数（linters/testersの並列実行数の上限）
    "jobs": 4,
    # flake8風無視パターン。
    "exclude": [
        # ここの値はflake8やblackなどの既定値を元に適当に。
        # https://github.com/github/gitignore/blob/master/Python.gitignore
        # https://github.com/github/gitignore/blob/main/Node.gitignore
        "*.egg",
        "*.egg-info",
        ".bzr",
        ".cache",
        ".direnv",
        ".eggs",
        ".git",
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
        "site",
        "venv",
    ],
    "extend-exclude": [],
    # コマンド名のエイリアス
    "aliases": {
        "format": ["pyupgrade", "autoflake", "isort", "black", "ruff-format"],
        "lint": ["ruff-check", "pflake8", "mypy", "pylint", "pyright", "ty", "markdownlint", "textlint"],
        "test": ["pytest"],
    },
}
"""デフォルト設定。"""


@dataclasses.dataclass(frozen=True)
class Config:
    """pyfltr設定。"""

    values: dict[str, typing.Any]
    commands: dict[str, CommandInfo]
    """ビルトイン + カスタムの統合コマンドレジストリ"""
    command_names: list[str]
    """コマンドの並び順リスト（ビルトイン順 → カスタムコマンド順）"""

    def __getitem__(self, key: str) -> typing.Any:
        """設定値を取得。"""
        return self.values[key]


def create_default_config() -> Config:
    """デフォルト設定を生成。"""
    config = Config(
        values=copy.deepcopy(DEFAULT_CONFIG),
        commands=dict(BUILTIN_COMMANDS),
        command_names=list(BUILTIN_COMMAND_NAMES),
    )
    config.values["aliases"]["fast"] = _build_fast_alias(config)
    return config


def load_config() -> Config:
    """pyproject.tomlから設定を読み込み。"""
    config = create_default_config()
    pyproject_path = pathlib.Path("pyproject.toml").absolute()
    if not pyproject_path.exists():
        return config

    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    tool_pyfltr = pyproject_data.get("tool", {}).get("pyfltr", {})

    # プリセットの反映 (CONFIGに直接)
    preset = str(tool_pyfltr.get("preset", ""))
    if preset == "":
        pass
    elif preset in ("20260330", "latest"):
        # ruff + pyright + textlint + markdownlint使用のプリセット
        config.values["pyupgrade"] = False
        config.values["autoflake"] = False
        config.values["pflake8"] = False
        config.values["isort"] = False
        config.values["black"] = False
        config.values["ruff-format"] = True
        config.values["ruff-check"] = True
        config.values["pyright"] = True
        config.values["textlint"] = True
        config.values["markdownlint"] = True
    elif preset == "20250710":
        # 旧プリセット（互換性維持）
        config.values["pyupgrade"] = False
        config.values["autoflake"] = False
        config.values["pflake8"] = False
        config.values["isort"] = False
        config.values["black"] = False
        config.values["ruff-format"] = True
        config.values["ruff-check"] = True
    else:
        raise ValueError(f"preset の設定値が正しくありません。{preset=}")

    # カスタムコマンドの読み込み
    custom_commands = tool_pyfltr.get("custom-commands", tool_pyfltr.get("custom_commands", {}))
    if not isinstance(custom_commands, dict):
        raise ValueError("custom-commandsはテーブルで指定してください")
    for name, definition in custom_commands.items():
        name = name.replace("_", "-")
        _register_custom_command(config, name, definition)

    # プリセット以外の設定を適用 (プリセットと重複があれば上書き)
    for key, value in tool_pyfltr.items():
        key = key.replace("_", "-")  # 「_」区切りと「-」区切りのどちらもOK
        if key in ("custom-commands",):
            continue  # カスタムコマンドは別途処理済み
        if key not in config.values:
            raise ValueError(f"設定キーが不正です: {key}")
        if not isinstance(value, type(config.values[key])):  # 簡易チェック
            raise ValueError(f"設定値が不正です: {key}={type(value)}, expected {type(config.values[key])}")
        config.values[key] = value

    # per-command fastフラグからfastエイリアスを再計算
    config.values["aliases"]["fast"] = _build_fast_alias(config)

    return config


def _register_custom_command(config: Config, name: str, definition: dict[str, typing.Any]) -> None:
    """カスタムコマンドをConfigに登録。"""
    # 名前衝突チェック
    if name in BUILTIN_COMMANDS:
        raise ValueError(f"カスタムコマンド名がビルトインコマンドと衝突しています: {name}")

    # type (必須)
    cmd_type = definition.get("type")
    if cmd_type not in ("formatter", "linter", "tester"):
        raise ValueError(f"カスタムコマンド {name} のtypeが不正です: {cmd_type}")

    # path (省略時はコマンド名)
    path = definition.get("path", name)
    if not isinstance(path, str):
        raise ValueError(f"カスタムコマンド {name} のpathは文字列で指定してください")

    # args (省略時は空リスト)
    args = definition.get("args", [])
    if not isinstance(args, list):
        raise ValueError(f"カスタムコマンド {name} のargsはリストで指定してください")

    # targets (省略時は "*.py")
    targets = definition.get("targets", "*.py")
    if not isinstance(targets, str):
        raise ValueError(f"カスタムコマンド {name} のtargetsは文字列で指定してください")

    # error-pattern (省略可)
    error_pattern = definition.get("error-pattern", definition.get("error_pattern"))
    if error_pattern is not None:
        if not isinstance(error_pattern, str):
            raise ValueError(f"カスタムコマンド {name} のerror-patternは文字列で指定してください")
        _validate_error_pattern(name, error_pattern)

    # CommandInfoを登録
    config.commands[name] = CommandInfo(
        type=cmd_type,
        builtin=False,
        targets=targets,
        error_pattern=error_pattern,
    )
    config.command_names.append(name)

    # fast (省略時はFalse)
    fast = definition.get("fast", False)
    if not isinstance(fast, bool):
        raise ValueError(f"カスタムコマンド {name} のfastはboolで指定してください")

    # values辞書にデフォルト設定を追加
    config.values[name] = True
    config.values[f"{name}-path"] = path
    config.values[f"{name}-args"] = args
    config.values[f"{name}-fast"] = fast


def _build_fast_alias(config: Config) -> list[str]:
    """per-command fastフラグからfastエイリアスを動的構築。"""
    return [name for name in config.command_names if config.values.get(f"{name}-fast", False)]


def _validate_error_pattern(name: str, pattern: str) -> None:
    """error-patternのバリデーション。"""
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"カスタムコマンド {name} のerror-patternが不正な正規表現です: {e}") from e
    # 必須グループの確認
    groups = compiled.groupindex
    for required in ("file", "line", "message"):
        if required not in groups:
            raise ValueError(f"カスタムコマンド {name} のerror-patternに{required}グループが必要です")


def resolve_aliases(commands: list[str], config: Config) -> list[str]:
    """エイリアスを展開。"""
    # 最大10回まで再帰的に展開
    result: list[str] = []
    for _ in range(10):
        result = []
        resolved: bool = False
        for command in commands:
            command = command.strip()
            if command in config["aliases"]:
                for c in config["aliases"][command]:
                    if c not in result:  # 順番は維持しつつ重複排除
                        result.append(c)
                resolved = True
            else:
                if command not in result:  # 順番は維持しつつ重複排除
                    result.append(command)
        if not resolved:
            break
        commands = result
    result.sort(key=config.command_names.index)  # リスト順にソート
    return result


def generate_config_text(config: Config | None = None) -> str:
    """設定ファイルのサンプルテキストを生成。"""
    if config is None:
        config = create_default_config()
    return "[tool.pyfltr]\n" + "\n".join(
        f"{key} = " + repr(value).replace("'", '"').replace("True", "true").replace("False", "false")
        for key, value in config.values.items()
    )
