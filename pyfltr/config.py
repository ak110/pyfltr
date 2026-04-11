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
    targets: str | list[str] = "*.py"
    """対象ファイルパターン。単一の glob 文字列または glob のリスト。"""
    error_pattern: str | None = None
    """エラーパース用正規表現"""
    serial_group: str | None = None
    """直列実行グループ名。

    同一グループ名のコマンドは linters/testers の並列実行でも同時に走らないよう
    pyfltr 側で排他する。cargo 系は ``"cargo"``、dotnet 系は ``"dotnet"`` を指定し、
    ``target`` ディレクトリなどの内部ロック競合を避ける。
    """

    def target_globs(self) -> list[str]:
        """対象ファイルパターンをリスト形式で返す。"""
        if isinstance(self.targets, str):
            return [self.targets]
        return list(self.targets)


# ビルトインコマンド定義（順序が並び順を決める）
# JS ツール系の対象拡張子は主要なもののみ列挙。プロジェクト個別の拡張子は
# 呼び出し時のターゲット指定やユーザーのシェル glob で吸収する想定。
_JS_COMMON_TARGETS: list[str] = [
    "*.js",
    "*.jsx",
    "*.mjs",
    "*.cjs",
    "*.ts",
    "*.tsx",
    "*.mts",
    "*.cts",
]

BUILTIN_COMMANDS: dict[str, CommandInfo] = {
    "pyupgrade": CommandInfo(type="formatter"),
    "autoflake": CommandInfo(type="formatter"),
    "isort": CommandInfo(type="formatter"),
    "black": CommandInfo(type="formatter"),
    "ruff-format": CommandInfo(type="formatter"),
    "prettier": CommandInfo(
        type="formatter",
        targets=[
            *_JS_COMMON_TARGETS,
            "*.vue",
            "*.svelte",
            "*.json",
            "*.jsonc",
            "*.yaml",
            "*.yml",
            "*.md",
            "*.mdx",
            "*.css",
            "*.scss",
            "*.less",
            "*.html",
        ],
    ),
    "uv-sort": CommandInfo(type="formatter", targets="pyproject.toml"),
    # bin-runner対応ツール（formatter → linterの順）
    "shfmt": CommandInfo(type="formatter", targets="*.sh"),
    # Rust / .NET 言語ツール (formatter)。いずれも pass-filenames=False でcrate/solution
    # 全体を対象とする project-level 実行で動作する。serial_group により同一ツールチェイン
    # (cargo / dotnet) のコマンドは直列実行され、target ディレクトリ等のロック競合を回避する。
    "cargo-fmt": CommandInfo(type="formatter", targets="*.rs", serial_group="cargo"),
    "dotnet-format": CommandInfo(
        type="formatter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props", ".editorconfig"],
        serial_group="dotnet",
    ),
    "ec": CommandInfo(type="linter", targets="*"),
    "shellcheck": CommandInfo(type="linter", targets="*.sh"),
    "typos": CommandInfo(type="linter", targets="*"),
    "actionlint": CommandInfo(
        type="linter",
        targets=[".github/workflows/*.yaml", ".github/workflows/*.yml"],
    ),
    "ruff-check": CommandInfo(type="linter"),
    "pflake8": CommandInfo(type="linter"),
    "mypy": CommandInfo(type="linter"),
    "pylint": CommandInfo(type="linter"),
    "pyright": CommandInfo(type="linter"),
    "ty": CommandInfo(type="linter"),
    "markdownlint": CommandInfo(type="linter", targets="*.md"),
    "textlint": CommandInfo(type="linter", targets="*.md"),
    "eslint": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.vue", "*.svelte"],
    ),
    "biome": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.json", "*.jsonc", "*.css"],
    ),
    "oxlint": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.vue", "*.svelte"],
    ),
    "tsc": CommandInfo(
        type="linter",
        targets=["*.ts", "*.tsx", "*.mts", "*.cts"],
    ),
    # Rust / .NET 言語ツール (linter)。pass-filenames=False で crate / solution 全体を対象とする。
    "cargo-clippy": CommandInfo(type="linter", targets=["*.rs", "Cargo.toml"], serial_group="cargo"),
    "cargo-check": CommandInfo(type="linter", targets=["*.rs", "Cargo.toml"], serial_group="cargo"),
    "cargo-deny": CommandInfo(
        type="linter",
        targets=["Cargo.toml", "Cargo.lock", "deny.toml"],
        serial_group="cargo",
    ),
    "dotnet-build": CommandInfo(
        type="linter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
    ),
    "pytest": CommandInfo(type="tester", targets="*_test.py"),
    # vitest のテストファイルパターン（pytest の *_test.py と同じ考え方）
    "vitest": CommandInfo(
        type="tester",
        targets=[
            "*.test.js",
            "*.test.jsx",
            "*.test.ts",
            "*.test.tsx",
            "*.spec.js",
            "*.spec.jsx",
            "*.spec.ts",
            "*.spec.tsx",
            "*.test.mjs",
            "*.test.mts",
            "*.test.cjs",
            "*.test.cts",
            "*.spec.mjs",
            "*.spec.mts",
            "*.spec.cjs",
            "*.spec.cts",
        ],
    ),
    # Rust / .NET 言語ツール (tester)。pass-filenames=False で crate / solution 全体を対象とする。
    "cargo-test": CommandInfo(type="tester", targets=["*.rs", "Cargo.toml"], serial_group="cargo"),
    "dotnet-test": CommandInfo(
        type="tester",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
    ),
}

BUILTIN_COMMAND_NAMES: list[str] = list(BUILTIN_COMMANDS.keys())
"""ビルトインコマンドの名前リスト。"""


JS_RUNNERS: tuple[str, ...] = ("pnpx", "pnpm", "npm", "npx", "yarn", "direct")
"""textlint / markdownlint の起動方式として指定できる値。"""

BIN_RUNNERS: tuple[str, ...] = ("direct", "mise")
"""ec / shellcheck 等のネイティブバイナリツールの起動方式として指定できる値。"""

PYTHON_COMMANDS: tuple[str, ...] = (
    "pyupgrade",
    "autoflake",
    "isort",
    "black",
    "ruff-format",
    "ruff-check",
    "pflake8",
    "mypy",
    "pylint",
    "pyright",
    "ty",
    "pytest",
    "uv-sort",
)
"""python = false で一括無効化されるコマンドの一覧。"""

AUTO_ARGS: dict[str, list[tuple[str, list[str]]]] = {
    "pylint": [
        ("pylint-pydantic", ["--load-plugins=pylint_pydantic"]),
    ],
    "mypy": [
        ("mypy-unused-awaitable", ["--enable-error-code=unused-awaitable"]),
    ],
}
"""コマンドごとの自動引数マッピング。

各タプルは (設定キー, 引数リスト) の対。設定キーが True の場合、
引数リストをコマンドライン先頭に自動挿入する。ユーザーの *-args と
重複する場合はスキップする。
"""


DEFAULT_CONFIG: dict[str, typing.Any] = {
    # プリセット
    "preset": "",
    # Python 系ツールの一括有効/無効。False にすると PYTHON_COMMANDS に
    # 列挙されたコマンドをすべて無効化する。個別設定で上書き可能。
    "python": True,
    # 自動オプション: 各ツールの望ましい引数を自動挿入する。
    # *-args とは独立して動作し、重複排除される。False で無効化可能。
    "pylint-pydantic": True,
    "mypy-unused-awaitable": True,
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
    # ネイティブバイナリツール (Go/Rust/Haskell 製等) の起動方式:
    # - mise: mise exec <tool>@<version> -- <cmd>（既定）
    # - direct: PATH 上のバイナリを直接実行
    "bin-runner": "mise",
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
    "textlint-packages": [
        "textlint-rule-preset-ja-technical-writing",
        "textlint-rule-preset-jtf-style",
        "textlint-rule-ja-no-abusage",
    ],
    "textlint-fast": True,
    # fix モード時に通常 args の後に追加する引数。
    # textlint は --fix で autofix 可能なルールを in-place 修正する。
    "textlint-fix-args": ["--fix"],
    "eslint": False,
    # path が空文字の場合は js-runner 設定に基づいて自動解決する。
    "eslint-path": "",
    # --format json は lint / fix 両モードで有効にする必要があるため共通 args に置く。
    # ESLint 9 系以降で compact / unix / tap などのコアフォーマッタが除去されたため、
    # 残っているコアフォーマッタのうち機械可読な json を採用している。
    "eslint-args": ["--format", "json"],
    "eslint-fast": False,
    # fix モード時に通常 args の後に追加する引数。eslint は --fix で autofix する。
    "eslint-fix-args": ["--fix"],
    "prettier": False,
    "prettier-path": "",
    "prettier-args": [],
    # prettier は --check (read-only) と --write (書き込み) が排他のため、
    # pyfltr は 2 段階で実行する。詳細は command.py の _execute_prettier_two_step を参照。
    "prettier-check-args": ["--check"],
    "prettier-write-args": ["--write"],
    "prettier-fast": True,
    "uv-sort": False,
    "uv-sort-path": "uv-sort",
    "uv-sort-args": [],
    "uv-sort-fast": True,
    "biome": False,
    "biome-path": "",
    # "check" サブコマンドと --reporter=github は lint / fix 両モードで有効にする
    # 必要があるため共通 args に置く。builtin パーサが GitHub workflow annotation
    # 形式を前提にしているため reporter の切り替えは注意が必要。
    "biome-args": ["check", "--reporter=github"],
    "biome-fast": True,
    # fix モード時に通常 args の後に追加する引数。
    # `biome check --write` で safe fix のみ適用する (--unsafe は含めない)。
    "biome-fix-args": ["--write"],
    # -- js-runner対応ツール（追加分） --
    "oxlint": False,
    "oxlint-path": "",
    "oxlint-args": [],
    "oxlint-fast": True,
    "tsc": False,
    "tsc-path": "",
    "tsc-args": ["--noEmit"],
    "tsc-pass-filenames": False,
    "tsc-fast": False,
    # -- Rust 言語ツール --
    # いずれも pass-filenames=False で crate 全体を対象とする project-level 実行。
    # cargo は version pin ではなく mise shim 経由で PATH に入る前提のため、
    # 専用 runner は使わず path に直接 "cargo" を指定する。
    "cargo-fmt": False,
    "cargo-fmt-path": "cargo",
    # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
    "cargo-fmt-args": ["fmt"],
    "cargo-fmt-pass-filenames": False,
    "cargo-fmt-fast": True,
    "cargo-clippy": False,
    "cargo-clippy-path": "cargo",
    # args は lint / fix 両モードで共通の前半部分。trailing flag (-- -D warnings)
    # は lint-args / fix-args の双方に重複して置き、--fix 時には `--fix` を
    # 中間に挿入できるよう分離している。
    "cargo-clippy-args": ["clippy", "--all-targets"],
    "cargo-clippy-lint-args": ["--", "-D", "warnings"],
    "cargo-clippy-fix-args": ["--fix", "--allow-staged", "--allow-dirty", "--", "-D", "warnings"],
    "cargo-clippy-pass-filenames": False,
    "cargo-clippy-fast": True,
    "cargo-check": False,
    "cargo-check-path": "cargo",
    "cargo-check-args": ["check", "--all-targets"],
    "cargo-check-pass-filenames": False,
    "cargo-check-fast": False,
    "cargo-test": False,
    "cargo-test-path": "cargo",
    "cargo-test-args": ["test"],
    "cargo-test-pass-filenames": False,
    "cargo-test-fast": False,
    "cargo-deny": False,
    "cargo-deny-path": "cargo-deny",
    "cargo-deny-args": ["check"],
    "cargo-deny-pass-filenames": False,
    "cargo-deny-fast": False,
    # -- .NET 言語ツール --
    "dotnet-format": False,
    "dotnet-format-path": "dotnet",
    # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
    "dotnet-format-args": ["format"],
    "dotnet-format-pass-filenames": False,
    "dotnet-format-fast": True,
    "dotnet-build": False,
    "dotnet-build-path": "dotnet",
    "dotnet-build-args": ["build", "--nologo"],
    "dotnet-build-pass-filenames": False,
    "dotnet-build-fast": False,
    "dotnet-test": False,
    "dotnet-test-path": "dotnet",
    "dotnet-test-args": ["test", "--nologo"],
    "dotnet-test-pass-filenames": False,
    "dotnet-test-fast": False,
    # -- bin-runner対応ツール --
    "shfmt": False,
    "shfmt-path": "",
    "shfmt-args": [],
    # shfmt は prettier 同様の二段階実行。-l でチェック、-w で書き込み。
    "shfmt-check-args": ["-l"],
    "shfmt-write-args": ["-w"],
    "shfmt-version": "latest",
    "shfmt-fast": True,
    "ec": False,
    "ec-path": "",
    "ec-args": ["-format", "gcc", "-no-color"],
    "ec-version": "latest",
    "ec-fast": True,
    "shellcheck": False,
    "shellcheck-path": "",
    "shellcheck-args": ["-f", "gcc"],
    "shellcheck-version": "latest",
    "shellcheck-fast": True,
    "typos": False,
    "typos-path": "",
    "typos-args": ["--format", "brief"],
    "typos-version": "latest",
    "typos-fast": True,
    "actionlint": False,
    "actionlint-path": "",
    "actionlint-args": [],
    "actionlint-version": "latest",
    "actionlint-fast": True,
    "pytest": True,
    "pytest-path": "pytest",
    "pytest-args": [],
    "pytest-devmode": True,  # PYTHONDEVMODE=1をするか否か
    "pytest-fast": False,
    "vitest": False,
    "vitest-path": "",
    "vitest-args": ["run"],  # vitest は run サブコマンドが必須
    "vitest-fast": False,
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
    # 出力形式 ("text" / "jsonl")。jsonl は LLM 向け JSON Lines 出力。
    "output-format": "text",
    # --output-format の出力先ファイルパス。空文字のとき stdout。
    "output-file": "",
    # 最大並列数（linters/testersの並列実行数の上限）
    "jobs": 4,
    # flake8風無視パターン。
    "exclude": [
        # ここの値はflake8やblackなどの既定値を元に適当に。
        # https://github.com/github/gitignore/blob/master/Python.gitignore
        # https://github.com/github/gitignore/blob/main/Node.gitignore
        "*.egg",
        "*.egg-info",
        ".aider*",
        ".bzr",
        ".cache",
        ".cursor",
        ".direnv",
        ".eggs",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pnpm",
        ".pyre",
        ".pytest_cache",
        ".ruff_cache",
        ".serena",
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
    # .gitignore に記載されたファイルを除外するか否か（git check-ignore を使用）
    "respect-gitignore": True,
    # コマンド名のエイリアス
    "aliases": {
        "format": [
            "pyupgrade",
            "autoflake",
            "isort",
            "black",
            "ruff-format",
            "prettier",
            "uv-sort",
            "shfmt",
            "cargo-fmt",
            "dotnet-format",
        ],
        "lint": [
            "ruff-check",
            "pflake8",
            "mypy",
            "pylint",
            "pyright",
            "ty",
            "markdownlint",
            "textlint",
            "eslint",
            "biome",
            "ec",
            "shellcheck",
            "typos",
            "actionlint",
            "oxlint",
            "tsc",
            "cargo-clippy",
            "cargo-check",
            "cargo-deny",
            "dotnet-build",
        ],
        "test": ["pytest", "vitest", "cargo-test", "dotnet-test"],
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


# 全プリセットの基盤: 旧ツール無効化 + ruff有効化
_PRESET_BASE: dict[str, bool] = {
    "pyupgrade": False,
    "autoflake": False,
    "pflake8": False,
    "isort": False,
    "black": False,
    "ruff-format": True,
    "ruff-check": True,
}

# プリセット定義。新しいプリセットほど有効化ツールが増える増分構造。
_PRESETS: dict[str, dict[str, bool]] = {
    "20250710": _PRESET_BASE,
    "20260330": {**_PRESET_BASE, "pyright": True, "textlint": True, "markdownlint": True},
    "20260411": {
        **_PRESET_BASE,
        "pyright": True,
        "textlint": True,
        "markdownlint": True,
        "actionlint": True,
        "typos": True,
        "uv-sort": True,
    },
}
_PRESETS["latest"] = _PRESETS["20260411"]


def load_config(config_dir: pathlib.Path | None = None) -> Config:
    """pyproject.tomlから設定を読み込み。"""
    config = create_default_config()
    base = config_dir or pathlib.Path.cwd()
    pyproject_path = (base / "pyproject.toml").absolute()
    if not pyproject_path.exists():
        return config

    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    tool_pyfltr = pyproject_data.get("tool", {}).get("pyfltr", {})

    # プリセットの反映
    preset = str(tool_pyfltr.get("preset", ""))
    if preset == "":
        pass
    elif preset in _PRESETS:
        config.values.update(_PRESETS[preset])
    else:
        raise ValueError(f"preset の設定値が正しくありません。{preset=}")

    # カスタムコマンドの読み込み
    custom_commands = tool_pyfltr.get("custom-commands", tool_pyfltr.get("custom_commands", {}))
    if not isinstance(custom_commands, dict):
        raise ValueError("custom-commandsはテーブルで指定してください")
    for name, definition in custom_commands.items():
        name = name.replace("_", "-")
        _register_custom_command(config, name, definition)

    # python 設定の適用 (preset < python < 個別設定)
    # python = false なら PYTHON_COMMANDS を一括無効化する。
    # 後続の個別設定ループで mypy = true 等の上書きが可能。
    python_flag = tool_pyfltr.get("python", True)
    if not python_flag:
        for cmd in PYTHON_COMMANDS:
            config.values[cmd] = False

    # プリセット・python 以外の設定を適用 (プリセットと重複があれば上書き)
    skip_keys = ("custom-commands", "python")
    targets_overrides: dict[str, str | list[str]] = {}
    extend_targets_map: dict[str, str | list[str]] = {}
    for key, value in tool_pyfltr.items():
        key = key.replace("_", "-")  # 「_」区切りと「-」区切りのどちらもOK
        if key in skip_keys:
            continue  # 別途処理済み
        # {command}-extend-targets の検出（長いサフィックスを先に判定）
        if key.endswith("-extend-targets"):
            cmd_name = key.removesuffix("-extend-targets")
            if cmd_name in config.commands:
                extend_targets_map[cmd_name] = _validate_targets_value(key, value)
                continue
        # {command}-targets の検出
        if key.endswith("-targets"):
            cmd_name = key.removesuffix("-targets")
            if cmd_name in config.commands:
                targets_overrides[cmd_name] = _validate_targets_value(key, value)
                continue
        if key not in config.values:
            raise ValueError(f"設定キーが不正です: {key}")
        if not isinstance(value, type(config.values[key])):  # 簡易チェック
            raise ValueError(f"設定値が不正です: {key}={type(value)}, expected {type(config.values[key])}")
        config.values[key] = value

    # targets の完全上書き
    for cmd_name, new_targets in targets_overrides.items():
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=new_targets)

    # extend-targets の追加（targets上書き後に適用）
    for cmd_name, extra in extend_targets_map.items():
        existing = config.commands[cmd_name].target_globs()
        if isinstance(extra, str):
            existing.append(extra)
        else:
            existing.extend(extra)
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=existing)

    # js-runner の値バリデーション
    js_runner = config.values["js-runner"]
    if js_runner not in JS_RUNNERS:
        raise ValueError(f"js-runnerの設定値が正しくありません。{js_runner=} (許容値: {', '.join(JS_RUNNERS)})")

    # bin-runner の値バリデーション
    bin_runner = config.values["bin-runner"]
    if bin_runner not in BIN_RUNNERS:
        raise ValueError(f"bin-runnerの設定値が正しくありません。{bin_runner=} (許容値: {', '.join(BIN_RUNNERS)})")

    # output-format の値バリデーション
    output_format = config.values["output-format"]
    if output_format not in ("text", "jsonl"):
        raise ValueError(f"output-formatの設定値が正しくありません。{output_format=} (許容値: text, jsonl)")

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

    # targets (省略時は "*.py"、str または list[str])
    raw_targets: typing.Any = definition.get("targets", "*.py")
    targets: str | list[str]
    if isinstance(raw_targets, str):
        targets = raw_targets
    elif isinstance(raw_targets, list) and all(isinstance(item, str) for item in raw_targets):
        # raw_targets は typing.Any 経由のため list(raw_targets) の要素型が縮まらない。
        # 上記 isinstance で要素が str であることを検証済みなので、明示的に str 化して
        # list[str] を構築する。
        targets = [str(item) for item in raw_targets]
    else:
        raise ValueError(f"カスタムコマンド {name} のtargetsは文字列または文字列のリストで指定してください")

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

    # pass-filenames (省略時はTrue)
    pass_filenames = definition.get("pass-filenames", definition.get("pass_filenames", True))
    if not isinstance(pass_filenames, bool):
        raise ValueError(f"カスタムコマンド {name} のpass-filenamesはboolで指定してください")

    # values辞書にデフォルト設定を追加
    config.values[name] = True
    config.values[f"{name}-path"] = path
    config.values[f"{name}-args"] = args
    config.values[f"{name}-fast"] = fast
    config.values[f"{name}-pass-filenames"] = pass_filenames
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


def _validate_targets_value(key: str, value: typing.Any) -> str | list[str]:
    """Targets / extend-targets の値をバリデーション。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    raise ValueError(f"{key}は文字列または文字列のリストで指定してください")


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
