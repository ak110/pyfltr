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
    """コマンドの種類（`formatter` / `linter` / `tester`）"""
    builtin: bool = True
    """ビルトインコマンドか否か"""
    targets: str | list[str] = "*.py"
    """対象ファイルパターン。単一のglob文字列またはglobのリスト。"""
    error_pattern: str | None = None
    """エラーパース用正規表現"""
    serial_group: str | None = None
    """直列実行グループ名。

    同一グループ名のコマンドはlinters/testersの並列実行でも同時に実行されないよう
    pyfltr側で排他する。cargo系は`"cargo"`、dotnet系は`"dotnet"`を指定し、
    `target`ディレクトリなどの内部ロック競合を避ける。
    """
    fixed_cost: float = 0.0
    """推定固定コスト（秒）。並列実行のスケジューリングに使用する。"""
    per_file_cost: float = 0.0
    """推定ファイルあたりコスト（秒/file）。並列実行のスケジューリングに使用する。"""
    config_files: list[str] = dataclasses.field(default_factory=list)
    """このコマンドの設定ファイル候補（glob可）。

    非空かつプロジェクトルートにいずれもマッチしないとき、`load_config`が警告を発行する。
    pre-commitのような「設定ファイル不在だと機能しない」ツールの設定不備を可視化する用途。
    `cacheable=True`のコマンドでは、ここに列挙した設定ファイルの内容hashもキャッシュキーに
    含める（設定変更時の誤ヒットを避けるため）。
    """
    cacheable: bool = False
    """ファイル hash キャッシュの対象にするか否か。

    `True`を指定できるのは「ファイル間依存を持たず、設定ファイルもCWDで完結し、
    書き込みを伴わないlinter」に限られる。新ツール追加時の判断ミスを防ぐため既定は
    `False`とし、対象ツールのみ明示的に`True`を指定する。
    """

    def target_globs(self) -> list[str]:
        """対象ファイルパターンをリスト形式で返す。"""
        if isinstance(self.targets, str):
            return [self.targets]
        return list(self.targets)


# ビルトインコマンド定義（順序が並び順を決める）
# 並び順方針は`.claude/rules/order.md`に集約。
# JSツール系の対象拡張子は主要なもののみ列挙。プロジェクト個別の拡張子は
# 呼び出し時のターゲット指定やユーザーのシェルglobで吸収する想定。
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
    # formatter群: 純粋formatter先頭（prettier）→ 中段（決定論的整形）→ 末尾（pre-commit）。
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
    "ruff-format": CommandInfo(type="formatter", fixed_cost=0.02),
    "uv-sort": CommandInfo(type="formatter", targets="pyproject.toml", fixed_cost=0.2),
    # bin-runner対応ツール（formatter → linterの順）
    "shfmt": CommandInfo(type="formatter", targets="*.sh"),
    # taplo: Rust製TOMLフォーマッター/リンター。shfmtと同様の2段階実行（check → format）。
    # 既定で無効（opt-in）。
    "taplo": CommandInfo(type="formatter", targets="*.toml", fixed_cost=0.3),
    # Rust / .NETツール（formatter）。いずれもpass-filenames=FalseでCrate/solution
    # 全体を対象とするproject-level実行で動作する。serial_groupにより同一ツールチェイン
    # （cargo / dotnet）のコマンドは直列実行され、targetディレクトリ等のロック競合を回避する。
    "cargo-fmt": CommandInfo(type="formatter", targets="*.rs", serial_group="cargo", fixed_cost=1.0),
    "dotnet-format": CommandInfo(
        type="formatter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props", ".editorconfig"],
        serial_group="dotnet",
        fixed_cost=2.0,
    ),
    # pre-commitはリポジトリ固有チェックが幅広く実行されるため、他formatterの修正後に最後で呼ぶ。
    "pre-commit": CommandInfo(
        type="formatter",
        targets="*",
        config_files=[".pre-commit-config.yaml"],
    ),
    "ec": CommandInfo(type="linter", targets="*"),
    "shellcheck": CommandInfo(type="linter", targets="*.sh", per_file_cost=0.03),
    "typos": CommandInfo(type="linter", targets="*", fixed_cost=0.04, per_file_cost=0.007),
    "actionlint": CommandInfo(
        type="linter",
        targets=[".github/workflows/*.yaml", ".github/workflows/*.yml"],
        fixed_cost=0.2,
    ),
    # GitLab CI設定の構文検証。GitLab API経由でlintするためネットワーク・認証が必須。
    # 既定で無効（opt-in）とし、CIや初学者環境で誤って失敗しないようにする。
    "glab-ci-lint": CommandInfo(
        type="linter",
        targets=".gitlab-ci.yml",
        fixed_cost=1.0,
    ),
    # yamllint: Python製YAMLリンター。YAML全般を対象とする。既定で無効（opt-in）。
    "yamllint": CommandInfo(
        type="linter",
        targets=["*.yaml", "*.yml"],
        fixed_cost=0.1,
        per_file_cost=0.01,
    ),
    # hadolint: Haskell製Dockerfileリンター。bin-runner経由。既定で無効（opt-in）。
    "hadolint": CommandInfo(
        type="linter",
        targets=["Dockerfile", "Dockerfile.*", "*.Dockerfile"],
        fixed_cost=0.2,
        per_file_cost=0.05,
    ),
    # gitleaks: Goバイナリ。リポジトリ全体のシークレット検出。
    # pass-filenames=Falseで全体を対象とする。bin-runner経由。既定で無効（opt-in）。
    "gitleaks": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=1.0,
    ),
    # Python linter群はモダン順（後ろほど新しい）に並べる。実行順はLPT並列で別管理。
    "pylint": CommandInfo(type="linter", fixed_cost=1.75, per_file_cost=0.3),
    "mypy": CommandInfo(type="linter", fixed_cost=0.2, per_file_cost=0.12),
    "ruff-check": CommandInfo(type="linter", fixed_cost=0.01),
    "pyright": CommandInfo(type="linter", fixed_cost=0.8, per_file_cost=0.155),
    "ty": CommandInfo(type="linter", fixed_cost=0.05, per_file_cost=0.01),
    "markdownlint": CommandInfo(type="linter", targets="*.md", fixed_cost=0.9, per_file_cost=0.035),
    "textlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=2.3,
        per_file_cost=0.4,
        # textlintは対象ファイル単独で完結する解析を行い、設定ファイルもCLIから起動した場合は
        # CWD直下でのみ解決される（公式ドキュメントのconfiguring / ignore章に準拠）。
        # 以下はtextlintが自動で読み込む設定ファイルとignoreファイルの完全列挙。
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
    # JS/TS linter群はモダン順（後ろほど新しい）に並べる。
    "tsc": CommandInfo(
        type="linter",
        targets=["*.ts", "*.tsx", "*.mts", "*.cts"],
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
    # Rust / .NETツール（linter）。pass-filenames=FalseでCrate / solution全体を対象とする。
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
    # Rust / .NETツール（tester）。pass-filenames=FalseでCrate / solution全体を対象とする。
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


PYTHON_RUNNERS: tuple[str, ...] = ("direct", "uv", "uvx")
"""グローバル`python-runner`設定で指定できる値。

- `"direct"`: `shutil.which`で本体依存に同梱されたバイナリを直接呼ぶ
- `"uv"`:     cwdに`uv.lock`があり、かつ`uv`バイナリが利用可能な場合は
              `uv run --frozen <bin>`経由でプロジェクトのvenvにあるツールを呼ぶ。
              いずれかが満たされなければdirectへフォールバック
- `"uvx"`:    `uvx <bin>`形式でPyPI最新版を都度取得して起動する。
              `uv.lock`は参照せず、`{command}-version`設定とも連動しない
"""

JS_RUNNERS: tuple[str, ...] = ("pnpx", "pnpm", "npm", "npx", "yarn", "direct")
"""グローバル`js-runner`設定で指定できる値。"""

BIN_RUNNERS: tuple[str, ...] = ("direct", "mise")
"""グローバル`bin-runner`設定で指定できる値。"""

COMMAND_RUNNERS: tuple[str, ...] = (
    # カテゴリ委譲値（3値）
    "python-runner",
    "js-runner",
    "bin-runner",
    # 直接指定値（9値）
    "direct",
    "mise",
    "uv",
    "uvx",
    "pnpx",
    "pnpm",
    "npm",
    "npx",
    "yarn",
)
"""`{command}-runner`設定で指定できる値（対称12値）。

カテゴリ委譲値とper-tool直接指定値を対等な選択肢として並べる。
両者は対称で、利用者は委譲とper-toolオーバーライドを自由に選べる。

- カテゴリ委譲値（グローバル設定へ委譲）:
    - `"python-runner"`: グローバル`python-runner`設定（`direct` / `uv` / `uvx`）へ委譲する
    - `"js-runner"`:     グローバル`js-runner`設定（`pnpx` / `pnpm` / `npm` / `npx` / `yarn` / `direct`）へ委譲する
    - `"bin-runner"`:    グローバル`bin-runner`設定（`mise` / `direct`）へ委譲する
- 直接指定値（per-toolで実装ツールを直接指定）:
    - `"direct"`: `{command}-path`またはbin名で直接実行する
    - `"mise"`:   `mise exec <backend>@<version> -- <bin>`で実行する
    - `"uv"`:     cwdに`uv.lock`があり、かつ`uv`バイナリが利用可能な場合は
                  `uv run --frozen <bin>`経由で起動する。いずれかが満たされなければdirectへフォールバック
    - `"uvx"`:    `uvx <bin>`形式でPyPI最新版を都度取得して起動する。
                  `uv.lock`は参照せず、`{command}-version`設定とも連動しない
    - `"pnpx"` / `"pnpm"` / `"npm"` / `"npx"` / `"yarn"`: 各JSパッケージマネージャー経由で起動する

カテゴリ横断の組み合わせ（例: Python系ツールに`pnpm`を指定）はバリデーションでは拒否しない。
無意味な組み合わせは実行時の解決ロジックがエラー終了する。
"""

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
"""python 設定に紐づく Python 系コマンドの一覧。

Python 系ツール一式は本体依存（`dependencies`）に同梱済みで、
`uvx pyfltr` 単発で揃う。`{command}-runner = "python-runner"` 既定（グローバル `python-runner = "uv"` 既定経由）により、
cwdに`uv.lock`がある場合は利用者プロジェクトのuv環境のツール版が優先される。

言語カテゴリキー`python`のgate対象で、preset内でTrueとなっているツールを
通過させる。`python = false`または未指定のときは、preset由来でTrueになった
コマンドも個別`{command} = true`指定がなければFalseに押し戻される。
個別`{command} = true`はgateを越えて優先される。
`ty`のみpreset非収録のため、使用時は個別に`ty = true`を指定する運用を維持する。"""

JAVASCRIPT_COMMANDS: tuple[str, ...] = (
    "eslint",
    "biome",
    "oxlint",
    "prettier",
    "tsc",
    "vitest",
)
"""javascript 設定に紐づく JavaScript / TypeScript 系コマンドの一覧。

TypeScriptはJavaScriptエコシステム上のツール群（eslint / prettier / tsc等）で
扱うため、専用カテゴリは設けずここに内包する。言語カテゴリキー`javascript`の
gate対象で、preset内でTrueとなっているツールを通過させる。挙動は
`PYTHON_COMMANDS`と同じ。"""

RUST_COMMANDS: tuple[str, ...] = (
    "cargo-fmt",
    "cargo-clippy",
    "cargo-check",
    "cargo-test",
    "cargo-deny",
)
"""rust 設定に紐づく Rust 系コマンドの一覧。

言語カテゴリキー`rust`のgate対象で、preset内でTrueとなっているツールを
通過させる。挙動は`PYTHON_COMMANDS`と同じ。"""

DOTNET_COMMANDS: tuple[str, ...] = (
    "dotnet-format",
    "dotnet-build",
    "dotnet-test",
)
"""dotnet 設定に紐づく .NET 系コマンドの一覧。

言語カテゴリキー`dotnet`のgate対象で、preset内でTrueとなっているツールを
通過させる。挙動は`PYTHON_COMMANDS`と同じ。"""

LANGUAGE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("python", PYTHON_COMMANDS),
    ("javascript", JAVASCRIPT_COMMANDS),
    ("rust", RUST_COMMANDS),
    ("dotnet", DOTNET_COMMANDS),
)
"""言語カテゴリキーと対応するコマンド群の対応表。

preset適用後のgate処理（カテゴリFalseのときpreset由来の該当コマンドTrueを
Falseに押し戻す）で共通に使う。"""

REMOVED_COMMANDS: frozenset[str] = frozenset({"pyupgrade", "autoflake", "isort", "black", "pflake8"})
"""v3.0.0 で削除されたコマンド名。

設定ファイル中に関連キー（`pyupgrade = true` / `black-args = [...]`など）を検出した
場合、`load_config`が案内付きのValueErrorを送出して移行を促す。"""

AUTO_ARGS: dict[str, list[tuple[str, list[str]]]] = {
    "pylint": [
        ("pylint-pydantic", ["--load-plugins=pylint_pydantic"]),
    ],
    "mypy": [
        ("mypy-unused-awaitable", ["--enable-error-code=unused-awaitable"]),
    ],
}
"""コマンドごとの自動引数マッピング。

各タプルは（設定キー, 引数リスト）の対。設定キーがTrueの場合、
引数リストをコマンドライン先頭に自動挿入する。ユーザーの`*-args`と
重複する場合はスキップする。
"""
