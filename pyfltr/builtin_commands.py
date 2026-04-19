"""ビルトインコマンド定義。"""
# pylint: disable=duplicate-code
# DEFAULT_CONFIG の aliases.lint や aliases.format は全言語横断で構成されるため、
# PYTHON_COMMANDS / JAVASCRIPT_COMMANDS 等の個別カテゴリとコマンド名が部分的に重複する。
# ファイル分割によって検出されるようになった必然的な重複であり、コード変更では解消しない。

import dataclasses
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
    fixed_cost: float = 0.0
    """推定固定コスト（秒）。並列実行のスケジューリングに使用する。"""
    per_file_cost: float = 0.0
    """推定ファイルあたりコスト（秒/file）。並列実行のスケジューリングに使用する。"""
    config_files: list[str] = dataclasses.field(default_factory=list)
    """このコマンドの設定ファイル候補（glob 可）。

    非空かつプロジェクトルートにいずれもマッチしないとき、``load_config`` が警告を発行する。
    pre-commit のような「設定ファイル不在だと機能しない」ツールの設定不備を可視化する用途。
    ``cacheable=True`` のコマンドでは、ここに列挙した設定ファイルの内容 hash もキャッシュキーに
    含める (設定変更時の誤ヒットを避けるため)。
    """
    cacheable: bool = False
    """ファイル hash キャッシュの対象にするか否か。

    ``True`` を指定できるのは「ファイル間依存を持たず、設定ファイルも CWD で完結し、
    書き込みを伴わない linter」に限られる。新ツール追加時の判断ミスを防ぐため既定は
    ``False`` とし、対象ツールのみ明示的に ``True`` を指定する。
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
    "pre-commit": CommandInfo(
        type="formatter",
        targets="*",
        config_files=[".pre-commit-config.yaml"],
    ),
    "ruff-format": CommandInfo(type="formatter", fixed_cost=0.02),
    "prettier": CommandInfo(
        type="formatter",
        fixed_cost=1.5,
        per_file_cost=0.02,
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
    "uv-sort": CommandInfo(type="formatter", targets="pyproject.toml", fixed_cost=0.2),
    # bin-runner対応ツール（formatter → linterの順）
    "shfmt": CommandInfo(type="formatter", targets="*.sh"),
    # Rust / .NET 言語ツール (formatter)。いずれも pass-filenames=False でcrate/solution
    # 全体を対象とする project-level 実行で動作する。serial_group により同一ツールチェイン
    # (cargo / dotnet) のコマンドは直列実行され、target ディレクトリ等のロック競合を回避する。
    "cargo-fmt": CommandInfo(type="formatter", targets="*.rs", serial_group="cargo", fixed_cost=1.0),
    "dotnet-format": CommandInfo(
        type="formatter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props", ".editorconfig"],
        serial_group="dotnet",
        fixed_cost=2.0,
    ),
    "ec": CommandInfo(type="linter", targets="*"),
    "shellcheck": CommandInfo(type="linter", targets="*.sh", per_file_cost=0.03),
    "typos": CommandInfo(type="linter", targets="*", fixed_cost=0.04, per_file_cost=0.007),
    "actionlint": CommandInfo(
        type="linter",
        targets=[".github/workflows/*.yaml", ".github/workflows/*.yml"],
        fixed_cost=0.2,
    ),
    "ruff-check": CommandInfo(type="linter", fixed_cost=0.01),
    "mypy": CommandInfo(type="linter", fixed_cost=0.2, per_file_cost=0.12),
    "pylint": CommandInfo(type="linter", fixed_cost=1.75, per_file_cost=0.3),
    "pyright": CommandInfo(type="linter", fixed_cost=0.8, per_file_cost=0.155),
    "ty": CommandInfo(type="linter", fixed_cost=0.05, per_file_cost=0.01),
    "markdownlint": CommandInfo(type="linter", targets="*.md", fixed_cost=0.9, per_file_cost=0.035),
    "textlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=2.3,
        per_file_cost=0.4,
        # textlint は対象ファイル単独で完結する解析を行い、設定ファイルも CLI から起動した場合は
        # CWD 直下でのみ解決される (公式ドキュメントの configuring / ignore 章に準拠)。
        # 以下は textlint が自動で読み込む設定ファイルとignoreファイルの完全列挙。
        config_files=[
            ".textlintrc",
            ".textlintrc.json",
            ".textlintrc.yml",
            ".textlintrc.yaml",
            ".textlintrc.js",
            ".textlintrc.cjs",
            "package.json",
            ".textlintignore",
        ],
        cacheable=True,
    ),
    "eslint": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.vue", "*.svelte"],
        fixed_cost=2.3,
        per_file_cost=0.05,
    ),
    "biome": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.json", "*.jsonc", "*.css"],
    ),
    "oxlint": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.vue", "*.svelte"],
        fixed_cost=0.7,
    ),
    "tsc": CommandInfo(
        type="linter",
        targets=["*.ts", "*.tsx", "*.mts", "*.cts"],
    ),
    # Rust / .NET 言語ツール (linter)。pass-filenames=False で crate / solution 全体を対象とする。
    "cargo-clippy": CommandInfo(type="linter", targets=["*.rs", "Cargo.toml"], serial_group="cargo", fixed_cost=3.0),
    "cargo-check": CommandInfo(type="linter", targets=["*.rs", "Cargo.toml"], serial_group="cargo", fixed_cost=2.0),
    "cargo-deny": CommandInfo(
        type="linter",
        targets=["Cargo.toml", "Cargo.lock", "deny.toml"],
        serial_group="cargo",
        fixed_cost=1.0,
    ),
    "dotnet-build": CommandInfo(
        type="linter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
        fixed_cost=5.0,
    ),
    "pytest": CommandInfo(type="tester", targets="*_test.py", fixed_cost=3.0),
    # vitest のテストファイルパターン（pytest の *_test.py と同じ考え方）
    "vitest": CommandInfo(
        type="tester",
        fixed_cost=3.0,
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
    "cargo-test": CommandInfo(type="tester", targets=["*.rs", "Cargo.toml"], serial_group="cargo", fixed_cost=3.0),
    "dotnet-test": CommandInfo(
        type="tester",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
        fixed_cost=5.0,
    ),
}

BUILTIN_COMMAND_NAMES: list[str] = list(BUILTIN_COMMANDS.keys())
"""ビルトインコマンドの名前リスト。"""


JS_RUNNERS: tuple[str, ...] = ("pnpx", "pnpm", "npm", "npx", "yarn", "direct")
"""textlint / markdownlint の起動方式として指定できる値。"""

BIN_RUNNERS: tuple[str, ...] = ("direct", "mise")
"""ec / shellcheck 等のネイティブバイナリツールの起動方式として指定できる値。"""

PYTHON_COMMANDS: tuple[str, ...] = (
    "ruff-format",
    "ruff-check",
    "mypy",
    "pylint",
    "pyright",
    "ty",
    "pytest",
    "uv-sort",
)
"""python 設定および `pyfltr[python]` extras に紐づく Python 系コマンドの一覧。

言語カテゴリキー ``python`` の gate 対象で、preset 内で True となっているツールを
通過させる。``python = false`` または未指定のときは、preset 由来で True になった
コマンドも個別 ``{command} = true`` 指定がなければ False に押し戻される。
個別 ``{command} = true`` は gate を越えて優先される。
``ty`` のみ preset 非収録のため、使用時は個別に ``ty = true`` を指定する運用を維持する。"""

JAVASCRIPT_COMMANDS: tuple[str, ...] = (
    "eslint",
    "biome",
    "oxlint",
    "prettier",
    "tsc",
    "vitest",
)
"""javascript 設定に紐づく JavaScript / TypeScript 系コマンドの一覧。

TypeScript は JavaScript エコシステム上のツール群（eslint / prettier / tsc 等）で
扱うため、専用カテゴリは設けずここに内包する。言語カテゴリキー ``javascript`` の
gate 対象で、preset 内で True となっているツールを通過させる。挙動は
``PYTHON_COMMANDS`` と同じ。"""

RUST_COMMANDS: tuple[str, ...] = (
    "cargo-fmt",
    "cargo-clippy",
    "cargo-check",
    "cargo-test",
    "cargo-deny",
)
"""rust 設定に紐づく Rust 系コマンドの一覧。

言語カテゴリキー ``rust`` の gate 対象で、preset 内で True となっているツールを
通過させる。挙動は ``PYTHON_COMMANDS`` と同じ。"""

DOTNET_COMMANDS: tuple[str, ...] = (
    "dotnet-format",
    "dotnet-build",
    "dotnet-test",
)
"""dotnet 設定に紐づく .NET 系コマンドの一覧。

言語カテゴリキー ``dotnet`` の gate 対象で、preset 内で True となっているツールを
通過させる。挙動は ``PYTHON_COMMANDS`` と同じ。"""

LANGUAGE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", PYTHON_COMMANDS),
    ("javascript", JAVASCRIPT_COMMANDS),
    ("rust", RUST_COMMANDS),
    ("dotnet", DOTNET_COMMANDS),
)
"""言語カテゴリキーと対応するコマンド群の対応表。

preset 適用後の gate 処理 (カテゴリ False のとき preset 由来の該当コマンド True を
False に押し戻す) で共通に使う。"""

REMOVED_COMMANDS: frozenset[str] = frozenset({"pyupgrade", "autoflake", "isort", "black", "pflake8"})
"""v3.0.0 で削除されたコマンド名。

設定ファイル中に関連キー (``pyupgrade = true`` / ``black-args = [...]`` など) を検出した
場合、``load_config`` が案内付きの ValueError を送出して移行を促す。"""

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
