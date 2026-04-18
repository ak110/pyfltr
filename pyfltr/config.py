"""設定関連の処理。"""
# pylint: disable=too-many-lines

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

既定ではすべて無効 (opt-in) となる。ユーザーが ``python = true`` を指定すると
一括で True に、個別に ``{command} = true`` を指定することでも有効化できる。"""

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
扱うため、専用カテゴリは設けずここに内包する。既定ではすべて無効 (opt-in)。
``javascript = true`` で一括有効化、個別に ``{command} = true`` でも有効化可能。"""

RUST_COMMANDS: tuple[str, ...] = (
    "cargo-fmt",
    "cargo-clippy",
    "cargo-check",
    "cargo-test",
    "cargo-deny",
)
"""rust 設定に紐づく Rust 系コマンドの一覧。

既定ではすべて無効 (opt-in)。``rust = true`` で一括有効化、個別に
``{command} = true`` でも有効化可能。"""

DOTNET_COMMANDS: tuple[str, ...] = (
    "dotnet-format",
    "dotnet-build",
    "dotnet-test",
)
"""dotnet 設定に紐づく .NET 系コマンドの一覧。

既定ではすべて無効 (opt-in)。``dotnet = true`` で一括有効化、個別に
``{command} = true`` でも有効化可能。"""

LANGUAGE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", PYTHON_COMMANDS),
    ("javascript", JAVASCRIPT_COMMANDS),
    ("rust", RUST_COMMANDS),
    ("dotnet", DOTNET_COMMANDS),
)
"""言語カテゴリ opt-in キーと対応するコマンド群の対応表。

preset 適用後の一括抑止および一括有効化のループで共通に使う。"""

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


DEFAULT_CONFIG: dict[str, typing.Any] = {
    # プリセット
    "preset": "",
    # 言語カテゴリ別の一括有効/無効。True にすると各 ``*_COMMANDS`` タプルに
    # 列挙されたコマンドをすべて有効化する。個別設定で上書き可能。
    # v3.0.0 で既定値を False (opt-in) に統一。対象外プロジェクトで言語別
    # linter が勝手に走るのを防ぐためで、Python 系は別途
    # ``pip install pyfltr[python]`` で依存を導入する必要がある。
    # JavaScript / Rust / .NET 系はそれぞれのツールチェインを前提とする。
    "python": False,
    "javascript": False,
    "rust": False,
    "dotnet": False,
    # pre-commit 統合。有効にすると pyfltr run/ci/fast 実行時に
    # pre-commit run --all-files を内部で呼び出す。
    # pre-commit-fast = True（既定）により fast も統合するため、
    # make format 相当の場面で pre-commit を別途呼ぶ必要がなくなる。
    # pre-commit 配下から pyfltr が起動された場合は PRE_COMMIT=1
    # 環境変数の検出により pre-commit 統合を自動でスキップする。
    "pre-commit": False,
    "pre-commit-path": "pre-commit",
    "pre-commit-args": ["run", "--all-files"],
    "pre-commit-pass-filenames": False,
    "pre-commit-fast": True,
    # .pre-commit-config.yaml から pyfltr 関連 hook を自動検出して SKIP する
    "pre-commit-auto-skip": True,
    # SKIP 環境変数に渡す hook ID の手動指定リスト（auto-skip と併用可能）
    "pre-commit-skip": [],
    # 自動オプション: 各ツールの望ましい引数を自動挿入する。
    # *-args とは独立して動作し、重複排除される。False で無効化可能。
    "pylint-pydantic": True,
    "mypy-unused-awaitable": True,
    # 構造化出力: 対応ツールの出力形式を JSON 等に切り替え、パーサーで
    # ルールコード・severity・fix 情報を構造化して取得する。
    # *-args とは独立した経路で注入されるため pyproject.toml の上書きに影響されない。
    "ruff-check-json": True,
    "pylint-json": True,
    "pyright-json": True,
    "pytest-tb-line": True,
    "shellcheck-json": True,
    "textlint-json": True,
    "typos-json": True,
    "eslint-json": True,
    "biome-json": True,
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
    # 言語カテゴリ (python / javascript / rust / dotnet) に属するツールは v3.0.0 で
    # opt-in 化したため、既定値は False。対応するカテゴリキー (``python = true`` 等)
    # または個別に ``{command} = true`` で有効化する。
    "mypy": False,
    "mypy-path": "mypy",
    "mypy-args": [],
    "mypy-fast": False,
    "pylint": False,
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
    # fix ステージ (pyfltr run / fast の自動修正段) で通常 args の後に追加する引数。
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
    # ESLint 9 系以降で compact / unix / tap などのコアフォーマッタが除去されたため、
    # 構造化出力は eslint-json 設定により _STRUCTURED_OUTPUT_SPECS 経由で注入する。
    "eslint-args": [],
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
    # "check" サブコマンドは共通 args に置く。--reporter=github は biome-json 設定
    # により _STRUCTURED_OUTPUT_SPECS 経由で注入する。
    "biome-args": ["check"],
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
    "pytest": False,
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
    # 実行アーカイブ (v3.0.0 追加)
    # 全実行のツール生出力・diagnostic 全件・実行メタをユーザーキャッシュ
    # (``platformdirs.user_cache_dir("pyfltr")``) へ保存する。CLI とは独立した
    # 詳細参照経路 (``show-run`` / ``list-runs``、MCP ツール) からいつでも
    # 全文を参照できるようにする。
    "archive": True,
    # 自動クリーンアップの閾値。いずれかを超過した時点で古い順に削除する。
    # 0 以下を指定するとその軸の自動削除は無効化される。
    "archive-max-runs": 100,
    "archive-max-size-mb": 1024,
    "archive-max-age-days": 30,
    # JSONL 出力の smart truncation 設定 (v3.0.0 追加)。
    # ``jsonl-diagnostic-limit`` はツール単位の diagnostic 出力件数上限。0 以下で無制限。
    # ``jsonl-message-max-lines`` / ``jsonl-message-max-chars`` は failed かつ diagnostics=0 のときの
    # tool.message (生出力末尾) を切り詰める閾値。
    # 切り詰めが発生しても、アーカイブ書き込みに成功していれば全文は ``tools/<tool>/output.log``
    # / ``tools/<tool>/diagnostics.jsonl`` から復元できる。アーカイブ無効時 / 初期化失敗時 /
    # 当該ツールの書き込み失敗時は切り詰めをスキップし JSONL に全文を出力する。
    "jsonl-diagnostic-limit": 0,
    "jsonl-message-max-lines": 30,
    "jsonl-message-max-chars": 2000,
    # ファイル hash キャッシュ (v3.0.0 パート D)。
    # ``CommandInfo.cacheable=True`` のツール (textlint) の実行結果をユーザーキャッシュへ保存し、
    # 同じ入力 (対象ファイル群・設定ファイル・実効コマンドライン等) が繰り返された場合に
    # ツール実行を省略して結果を復元する。エージェントが同じ markdown に対して textlint を
    # 繰り返し呼び出すワークフローでの待機時間を削減する用途。
    # ``--no-cache`` CLI フラグまたは ``cache = false`` 設定で無効化できる。
    # ``cache-max-age-hours`` は保存期間 (時間) で、短期破棄前提として既定 12 時間。
    # 0 以下で期間軸のクリーンアップを無効化する。
    "cache": True,
    "cache-max-age-hours": 12,
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
        # バイナリファイル（テキスト系lintの対象外）
        "*.bmp",
        "*.dll",
        "*.dylib",
        "*.eot",
        "*.exe",
        "*.gif",
        "*.gz",
        "*.ico",
        "*.jpeg",
        "*.jpg",
        "*.mp3",
        "*.mp4",
        "*.otf",
        "*.pdf",
        "*.png",
        "*.so",
        "*.tar",
        "*.ttf",
        "*.wasm",
        "*.wav",
        "*.webp",
        "*.woff",
        "*.woff2",
        "*.zip",
    ],
    "extend-exclude": [],
    # .gitignore に記載されたファイルを除外するか否か（git check-ignore を使用）
    "respect-gitignore": True,
    # コマンド名のエイリアス
    "aliases": {
        "format": [
            "pre-commit",
            "ruff-format",
            "prettier",
            "uv-sort",
            "shfmt",
            "cargo-fmt",
            "dotnet-format",
        ],
        "lint": [
            "ruff-check",
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


# プリセット定義。v3.0.0 で preset の役割を「言語非依存 + ドキュメント系」に
# 絞る方針へ変更した。各言語カテゴリ (python / javascript / rust / dotnet) は
# preset では有効化せず、利用側で opt-in キーを明示する運用とする。
# これにより非 Python / 非 JS プロジェクトで意図しないツールが走るのを防ぐ。
_PRESETS: dict[str, dict[str, bool]] = {
    "20260418": {
        "markdownlint": True,
        "textlint": True,
        "actionlint": True,
        "typos": True,
        "pre-commit": True,
    },
}
_PRESETS["latest"] = _PRESETS["20260418"]

# v3.0.0 で削除されたプリセット名と、移行先を示すメッセージの対応表。
# ``load_config`` が該当プリセット指定を検知したら案内付き ValueError を送出する。
_REMOVED_PRESETS: dict[str, str] = {
    "20250710": (
        'preset "20250710" は v3.0.0 で削除された。'
        "5 ツール削除 (pyupgrade / autoflake / isort / black / pflake8) に伴い、"
        '当該プリセットは実質的に内容を失ったため廃止された。代わりに `preset = "latest"` を使い、'
        "必要な Python 系ツールを ``python = true`` または個別設定で有効化すること"
    ),
    "20260330": (
        'preset "20260330" は v3.0.0 で削除された。'
        "preset 内容が言語非依存 + ドキュメント系のみに整理されたため、"
        '旧 preset "20260330" 相当を復元するには `preset = "latest"` を指定した上で '
        "`python = true` (または個別の ``pyright = true`` 等) を追加すること"
    ),
    "20260411": (
        'preset "20260411" は v3.0.0 で削除された。'
        "preset 内容が言語非依存 + ドキュメント系のみに整理されたため、"
        '旧 preset "20260411" 相当を復元するには `preset = "latest"` を指定した上で '
        "`python = true` (または個別の ``pyright = true`` / ``uv-sort = true`` 等) を追加すること"
    ),
    "20260413": (
        'preset "20260413" は v3.0.0 で削除された。'
        "preset 内容が言語非依存 + ドキュメント系のみに整理されたため、"
        '旧 preset "20260413" 相当を復元するには `preset = "latest"` を指定した上で '
        "`python = true` (または個別の ``pyright = true`` / ``uv-sort = true`` 等) を追加すること"
    ),
}


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
    elif preset in _REMOVED_PRESETS:
        raise ValueError(_REMOVED_PRESETS[preset])
    else:
        raise ValueError(f"preset の設定値が正しくありません。{preset=}")

    # カスタムコマンドの読み込み
    custom_commands = tool_pyfltr.get("custom-commands", tool_pyfltr.get("custom_commands", {}))
    if not isinstance(custom_commands, dict):
        raise ValueError("custom-commandsはテーブルで指定してください")
    for name, definition in custom_commands.items():
        name = name.replace("_", "-")
        _register_custom_command(config, name, definition)

    # 言語カテゴリ opt-in の適用 (preset < 言語カテゴリ < 個別設定)
    # v3.0.0 で python / javascript / rust / dotnet を同じ枠組みの opt-in キーに統一した。
    # preset では言語別ツールを有効化しないため、まず preset 由来で紛れ込んだ個別 True を
    # 抑止し (各言語カテゴリキーも個別キーも False の場合のみ強制 False)、その後でカテゴリ
    # キーが True なら該当コマンド群を一括有効化する。後続の個別設定ループで
    # ``{command} = false`` / ``{command} = true`` による上書きが可能。
    # 設定キーの「_」「-」ゆらぎに対応するため、ユーザー入力側のキー集合を正規化しておく。
    user_keys = {key.replace("_", "-") for key in tool_pyfltr}
    language_flags: dict[str, bool] = {}
    for category_key, commands in LANGUAGE_CATEGORIES:
        flag = bool(tool_pyfltr.get(category_key, False))
        language_flags[category_key] = flag
        if not flag:
            for cmd in commands:
                if cmd in user_keys:
                    continue  # 個別設定による明示指定を保持 (True/False 双方)
                config.values[cmd] = False
    for category_key, commands in LANGUAGE_CATEGORIES:
        if language_flags[category_key]:
            for cmd in commands:
                config.values[cmd] = True

    # プリセット・言語カテゴリ以外の設定を適用 (プリセットと重複があれば上書き)
    skip_keys = ("custom-commands", *(key for key, _ in LANGUAGE_CATEGORIES))
    targets_overrides: dict[str, str | list[str]] = {}
    extend_targets_map: dict[str, str | list[str]] = {}
    for key, value in tool_pyfltr.items():
        key = key.replace("_", "-")  # 「_」区切りと「-」区切りのどちらもOK
        if key in skip_keys:
            continue  # 別途処理済み
        # v3.0.0 で削除されたツール名に紐づく設定キーを検出したら移行案内を出す。
        # "pyupgrade" / "pyupgrade-path" / "pyupgrade-args" / "pyupgrade-fast" などを網羅する。
        removed_owner = _extract_removed_command(key)
        if removed_owner is not None:
            raise ValueError(
                f'"{key}" は v3.0.0 で削除されたツール "{removed_owner}" 向けの設定である。'
                "5 ツール (pyupgrade / autoflake / isort / black / pflake8) は ruff への統合により削除された。"
                "該当設定をすべて pyproject.toml から除去すること"
            )
        # {command}-exclude の検出
        if key.endswith("-exclude"):
            cmd_name = key.removesuffix("-exclude")
            if cmd_name in config.commands:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError(f"設定値が不正です: {key} はstr型のリストで指定してください")
                config.values[key] = value
                continue
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

    # per-command fastフラグからfastエイリアスを再計算
    config.values["aliases"]["fast"] = _build_fast_alias(config)

    # 有効化されているコマンドの config_files が見つからなければ警告
    _warn_missing_config_files(config, base)

    return config


def _warn_missing_config_files(config: Config, base: pathlib.Path) -> None:
    """有効化されているコマンドで ``CommandInfo.config_files`` を満たさないものを警告する。"""
    # 遅延 import で循環依存を避ける（warnings_ は pyfltr 内で広く参照されるため）
    import pyfltr.warnings_  # pylint: disable=import-outside-toplevel

    for command, info in config.commands.items():
        if not info.config_files:
            continue
        if config.values.get(command) is not True:
            continue
        if any(list(base.glob(pattern)) for pattern in info.config_files):
            continue
        candidates = ", ".join(info.config_files)
        pyfltr.warnings_.emit_warning(
            source="config",
            message=f"{command} が有効化されていますが、設定ファイルが見つかりません: {candidates}",
        )


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

    # config-files (省略可。設定ファイル候補の glob パターン)
    raw_config_files: typing.Any = definition.get("config-files", definition.get("config_files", []))
    if not isinstance(raw_config_files, list) or not all(isinstance(item, str) for item in raw_config_files):
        raise ValueError(f"カスタムコマンド {name} のconfig-filesは文字列のリストで指定してください")
    config_files: list[str] = [str(item) for item in raw_config_files]

    # CommandInfoを登録
    config.commands[name] = CommandInfo(
        type=cmd_type,
        builtin=False,
        targets=targets,
        error_pattern=error_pattern,
        config_files=config_files,
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


def _extract_removed_command(key: str) -> str | None:
    """設定キーが削除コマンド宛なら該当コマンド名を返す、そうでなければ None。

    ``"pyupgrade"`` のような bare key と、``"pyupgrade-path"`` / ``"pyupgrade-args"`` /
    ``"pyupgrade-fast"`` などの派生キーの双方を検出する。
    """
    if key in REMOVED_COMMANDS:
        return key
    for command in REMOVED_COMMANDS:
        if key.startswith(f"{command}-"):
            return command
    return None


def _validate_targets_value(key: str, value: typing.Any) -> str | list[str]:
    """Targets / extend-targets の値をバリデーション。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    raise ValueError(f"{key}は文字列または文字列のリストで指定してください")


def filter_fix_commands(commands: list[str], config: Config) -> list[str]:
    """Fix ステージで実行すべきコマンドに絞り込む。

    `pyfltr run` / `pyfltr fast` の fix ステージは linter の autofix 機能
    (`{command}-fix-args`) を前段で呼び出すための段で、formatter は対象外。
    formatter 本体は通常ステージで常に書き込みモードで動くため、fix ステージで
    重複して走らせる必要は無い。

    enabled かつ `{command}-fix-args` が定義されている linter/tester を返す。
    """
    result: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        if f"{command}-fix-args" in config.values:
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
