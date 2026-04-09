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


JS_RUNNERS: tuple[str, ...] = ("pnpx", "pnpm", "npm", "npx", "yarn", "direct")
"""textlint / markdownlint の起動方式として指定できる値。"""


DEFAULT_CONFIG: dict[str, typing.Any] = {
    # プリセット
    "preset": "",
    # textlint / markdownlint の起動方式。
    # textlint-path / markdownlint-path が空のときに、以下の値に従って
    # 実際の起動コマンドを組み立てる。
    # - pnpx: グローバル / キャッシュから実行 (既定。従来互換)
    # - pnpm: pnpm exec <cmd> (プロジェクトの node_modules を利用)
    # - npm:  npm exec --no -- <cmd>
    # - npx:  npx --no-install -- <cmd>
    # - yarn: yarn run <cmd>
    # - direct: node_modules/.bin/<cmd> を直接起動
    "js-runner": "pnpx",
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
    # path が空文字の場合は js-runner 設定に基づいて自動解決する。
    # ユーザーが明示的に path を設定した場合はその値をそのまま使い、args 先頭に自動 prefix を追加しない。
    "markdownlint-path": "",
    "markdownlint-args": [],
    "markdownlint-fast": True,
    # fix モード (pyfltr --fix) 時に通常 args の後に追加する引数。
    # markdownlint-cli2 は --fix でファイルを in-place 修正する。
    "markdownlint-fix-args": ["--fix"],
    "textlint": False,
    # path が空文字の場合は js-runner 設定に基づいて自動解決する。
    "textlint-path": "",
    # lint / fix 共通で常に付与される引数。lint 専用オプション (--format など) はここではなく
    # textlint-lint-args に書くこと。fix 時は @textlint/fixer-formatter が使用されるが
    # compact フォーマッタが存在しないため、--format compact を共通 args に含めると fix が失敗する。
    "textlint-args": [],
    # 非 fix モード (および fix モードの後段 lint チェック) でのみ付与する引数。
    # 既定は compact フォーマッタ指定 (builtin パーサが compact 出力をパースする前提のため)。
    "textlint-lint-args": ["--format", "compact"],
    # textlint 向けルール / プリセットパッケージの列挙。pnpx / npx モードでは
    # --package / -p 展開される。pnpm / npm / yarn / direct モードでは
    # package.json 側で管理する前提のため無視される。
    "textlint-packages": ["textlint-rule-preset-ja-technical-writing"],
    "textlint-fast": True,
    # fix モード時に通常 args の後に追加する引数。
    # textlint は --fix で autofix 可能なルールを in-place 修正する。
    "textlint-fix-args": ["--fix"],
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
    # fix モード時に通常 args の後に追加する引数。
    # `ruff check --fix --unsafe-fixes` で autofix 可能な違反を修正する。
    # (通常モードの ruff-format-by-check とは別経路で動作する)
    "ruff-check-fix-args": ["--fix", "--unsafe-fixes"],
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

    # js-runner の値バリデーション
    js_runner = config.values["js-runner"]
    if js_runner not in JS_RUNNERS:
        raise ValueError(f"js-runnerの設定値が正しくありません。{js_runner=} (許容値: {', '.join(JS_RUNNERS)})")

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

    # fix-args (省略可、省略時は fix モード非対応として扱う)
    fix_args = definition.get("fix-args", definition.get("fix_args"))
    if fix_args is not None and not isinstance(fix_args, list):
        raise ValueError(f"カスタムコマンド {name} のfix-argsはリストで指定してください")

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
    # fix-args は定義されている場合のみ登録する (キーの有無で fix 対応可否を判別)
    if fix_args is not None:
        config.values[f"{name}-fix-args"] = fix_args


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


def filter_fix_commands(commands: list[str], config: Config) -> list[str]:
    """Fix モードで実行すべきコマンドに絞り込む。

    enabled かつ次のいずれかを満たすコマンドを返す。
    - formatter (通常実行そのものがファイルを修正するため)
    - fix-args が定義されている linter/tester (fix-args 付きで起動すると修正される)
    """
    result: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        command_info = config.commands[command]
        if command_info.type == "formatter" or f"{command}-fix-args" in config.values:
            result.append(command)
    return result


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
