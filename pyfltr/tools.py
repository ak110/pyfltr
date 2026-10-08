"""ビルトインコマンド定義。"""

import dataclasses
import importlib
import sys
import typing

from pyfltr.diagnostics import FILE_PATTERN as _FILE
from pyfltr.diagnostics import ErrorLocation

_P = typing.ParamSpec("_P")
_R = typing.TypeVar("_R")


@dataclasses.dataclass(frozen=True)
class FunctionRef(typing.Generic[_P, _R]):
    """登録を読み込む際の循環を避け、関数を呼出時に解決する。"""

    module: str
    name: str

    def resolve(self) -> typing.Callable[_P, _R]:
        """登録先の関数を型付きで取得する。"""
        return typing.cast(typing.Callable[_P, _R], getattr(importlib.import_module(self.module), self.name))

    def __call__(self, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        """登録先へ同じ引数を渡し、結果を返す。"""
        return self.resolve()(*args, **kwargs)


@dataclasses.dataclass(frozen=True)
class BinToolSpec:
    """bin-runner対応ツール（ネイティブバイナリ）の解決情報。

    `_BIN_TOOL_SPEC` テーブルでpyfltrコマンド名と対応付けて登録する。
    `{command}-runner` が `"bin-runner"`（グローバル `bin-runner` へ委譲）または `"mise"` のとき、
    本specの `mise_backend` と `bin_name` から `mise exec ... -- <bin>` 形式のコマンドラインを組み立てる。
    `{command}-path` が非空ならその値が優先され、本テーブルは参照しない。
    新ツール追加時は`BUILTIN_COMMANDS`の同じ項目へ、本specとrunner・versionの既定値を置く。
    """

    bin_name: str
    """実行ファイル名"""
    mise_backend: str | None = None
    """mise exec用のbackend指定（省略時は `bin_name`）"""


@dataclasses.dataclass(frozen=True)
class StructuredOutputSpec:
    """構造化出力用の引数注入仕様。"""

    inject: list[str]
    """注入する引数"""
    conflicts: list[str]
    """commandlineから除去する引数プレフィクス"""
    lint_only: bool = False
    """Trueのときfixモードでは注入しない"""


ExecutionKind = typing.Literal[
    "plain", "precommit", "glab", "vitest", "textlint-fix", "linter-fix", "ruff-format", "prettier", "check-write"
]

CommandType = typing.Literal["formatter", "linter", "tester"]
"""コマンドの種類。"""


def command_index(command_names: typing.Sequence[str], command: str) -> int:
    """定義順の位置を返す。未登録コマンドは末尾へ置く。"""
    return command_names.index(command) if command in command_names else len(command_names)


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

    `--config`引数として渡す注入候補は`config_inject_candidates`へ別途列挙する。
    本フィールドの値は注入経路には使わない（`.textlintignore`や`package.json`など
    `--config`引数として渡せない要素を含み得るため、責務を分離している）。
    """
    config_arg_template: list[str] | None = None
    """設定ファイル絶対パスを注入するための引数テンプレート。

    `None`（既定）の場合、外部パス指定時の`--config`明示注入を行わない。
    指定する場合は`["--config", "{path}"]`のように、`{path}`プレースホルダーを
    含む文字列リストを渡す。`_prepare_execution_params`が起点cwd直下から
    `config_inject_candidates`を順に探索し、最初に見つかった設定ファイルの
    絶対パスを`{path}`へ置換して`commandline_prefix`直後に挿入する。
    起点cwd直下に候補が見つからないときは注入をスキップしてツールの既定動作に委ねる。

    対象ファイルが起点cwd配下のみのときも一律で注入経路を通し、
    内部パス／外部パス混在で挙動差が出ないようにする。
    `allows_external_paths=False`を併用するツール（`markdownlint`・`textlint`）では
    既定で外部パスが本経路へ到達する前に除外される。
    利用者がCLIの`--allow-external-paths`を指定した場合は外部パスが除外されず、
    外部パスに対しても起点cwd基準で解決した設定ファイルを注入する。
    非モノレポ経路では`_prepare_execution_params`が、モノレポ経路では
    `run_subproject_loop`が起点cwdで行う外部パス専用の追加実行が、同じ注入経路を通る。
    """
    config_inject_candidates: list[str] = dataclasses.field(default_factory=list)
    """`--config`引数として渡せる設定ファイル候補（順序＝探索優先順）。

    `config_arg_template`が指定されたツールで起点cwd直下を本リスト順に走査し、
    最初に見つかったファイルの絶対パスを注入する。
    `config_files`（自動読込候補の完全列挙）とは別フィールドとし、
    `--config`引数として受け付け可能な候補のみを列挙する。
    """
    allows_external_paths: bool = True
    """起点cwd配下にない絶対パス（外部パス）を実行対象として許容するか否か。

    `True`（既定）の場合、外部パスもそのまま対象に含めて起動する。
    `False`の場合、`_prepare_execution_params`が対象から外部パスを除外し、
    各ファイルに対して警告を発行（`pyfltr.warnings_.emit_warning`）し、
    `pyfltr.warnings_.add_filtered_direct_file(reason="external")`へ蓄積する。
    pre-commit・tester系（`pytest`・`vitest`・`cargo-test`・`dotnet-test`）・
    リポジトリ全体走査型（`gitleaks`・`semgrep`）・
    プロジェクト単位で起動する依存の脆弱性監査ツール（`uv-audit`・`pnpm-audit`・`npm-audit`・`yarn-audit`）等、
    リポジトリ外ファイルを渡すと想定外動作となるツールで`False`を指定する。

    利用者がCLIの`--allow-external-paths`を指定した場合、本フィールドの値にかかわらず
    除外と警告を行わず外部パスを対象へ含める。既定の安全側動作を利用者責任で外す
    エスケープハッチとして`--no-exclude`と同格に扱う。非モノレポ経路では
    `_prepare_execution_params`が外部パスを保持し、モノレポ経路では
    `run_subproject_loop`が起点cwdで外部パス専用の追加実行を行う。
    """
    cacheable: bool = False
    """ファイル hash キャッシュの対象にするか否か。

    `True`を指定できるのは「ファイル間依存を持たず、設定ファイルもCWDで完結し、
    書き込みを伴わないlinter」に限られる。新ツール追加時の判断ミスを防ぐため既定は
    `False`とし、対象ツールのみ明示的に`True`を指定する。
    """
    subproject_aware: bool = True
    """モノレポ検出時にサブプロジェクト単位で分割実行するか否か。

    `True`（既定）の場合、起点 cwd 配下に複数のマーカー
    （`pyproject.toml`・`Cargo.toml`・`*.csproj`・`*.sln`）を検出した時に、
    対象ファイルをサブプロジェクト別に分類してそれぞれのcwdでツールを起動する。
    Python系・JS系・Rust系・.NET系・各種linter/formatter/testerなど、
    プロジェクトローカル設定・モジュール解決・lockfileをcwd起点で読むツールが該当する。

    `False` の場合、サブプロジェクトを区別せず起点 cwd で1回起動する。
    `typos`・`shellcheck`・`shfmt`・`pre-commit` 等、リポジトリ単位で動作する
    ツールが該当する。

    マーカーを1件しか検出しない単一プロジェクト時は、フラグ値に
    関わらず従来通り起点 cwd で1回起動する。利用者は
    `{command}-subproject-aware` 設定キーで個別に上書きできる。
    """

    execution_kind: ExecutionKind = "plain"
    fix_execution_kind: ExecutionKind = "linter-fix"
    execution_enabled_key: str | None = None
    language: str | None = None
    aliases: tuple[str, ...] = ()
    auto_args: list[tuple[str, list[str]]] = dataclasses.field(default_factory=list)
    auto_value_args: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    defaults: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    js_bin: str | None = None
    python_bin: str | None = None
    package_manager_bin: str | None = None
    minimum_version: tuple[tuple[int, ...], str] | None = None
    pnpx_package: str | None = None
    bin_spec: BinToolSpec | None = None
    structured_output: tuple[str, StructuredOutputSpec] | None = None
    diagnostic_pattern: str | None = None
    parser: typing.Callable[[str], list[ErrorLocation]] | None = None
    path_base_parser: typing.Callable[..., list[ErrorLocation]] | None = None
    summary_parser: typing.Callable[[str], str | None] | None = None
    rule_hints: dict[str, str] = dataclasses.field(default_factory=dict)
    rule_url_builder: typing.Callable[[str, str | None], str | None] | None = None

    def require_diagnostic_pattern(self) -> str:
        """正規表現へのフォールバックを持つ解析処理へ登録済みパターンを返す。"""
        if self.diagnostic_pattern is None:
            raise ValueError("診断パターンが登録されていない")
        return self.diagnostic_pattern

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
    # formatter群: 純粋formatter先頭（prettier）→ 中段（決定論的整形）→ 末尾（prek→pre-commit）。
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
        js_bin="prettier",
        language="javascript",
        defaults={
            "prettier": False,
            "prettier-path": "",
            "prettier-runner": "js-runner",
            "prettier-args": [],
            # prettierは--check（read-only）と--write（書き込み）が排他のため、
            # pyfltrは2段階で実行する。詳細はcommand.pyの`execute_prettier_two_step`を参照。
            "prettier-check-args": ["--check"],
            "prettier-write-args": ["--write"],
            "prettier-fast": True,
        },
        execution_kind="prettier",
    ),
    "ruff-format": CommandInfo(
        type="formatter",
        fixed_cost=0.02,
        python_bin="ruff",
        language="python",
        defaults={
            "ruff-format": False,
            "ruff-format-path": "",
            "ruff-format-runner": "python-runner",
            "ruff-format-args": ["format", "--exit-non-zero-on-format"],
            "ruff-format-fast": True,
            # ruff-format実行時にruff check --fix --unsafe-fixesを先に実行するか。
            # 既定では有効とし、未整形のimportソートや安全に自動修正できるlint違反を
            # フォーマットと一緒に片付ける（ruff公式推奨ワークフローの発展形）。
            # unsafe fix採用方針はbiome側（`biome-fix-args`）と統一する。
            # lintエラーは別途ruff-checkで検出される前提のため、ステップ1の
            # lint violation（exit 1）はruff-format側では失敗扱いしない。
            "ruff-format-by-check": True,
            "ruff-format-check-args": ["check", "--fix", "--unsafe-fixes"],
        },
        execution_kind="ruff-format",
        execution_enabled_key="ruff-format-by-check",
    ),
    "uv-sort": CommandInfo(
        type="formatter",
        targets="pyproject.toml",
        fixed_cost=0.2,
        python_bin="uv-sort",
        language="python",
        defaults={
            "uv-sort": False,
            "uv-sort-path": "",
            "uv-sort-args": [],
            "uv-sort-runner": "python-runner",
            "uv-sort-fast": True,
        },
    ),
    # bin-runner対応ツール（formatter → linterの順）
    "shfmt": CommandInfo(
        type="formatter",
        targets="*.sh",
        subproject_aware=False,
        bin_spec=BinToolSpec(bin_name="shfmt"),
        defaults={
            # -- bin-runner対応ツール --
            "shfmt": False,
            "shfmt-path": "",
            "shfmt-runner": "bin-runner",
            "shfmt-args": [],
            # shfmt は prettier 同様の二段階実行。-l でチェック、-w で書き込み。
            "shfmt-check-args": ["-l"],
            "shfmt-write-args": ["-w"],
            "shfmt-version": "latest",
            "shfmt-fast": True,
        },
        execution_kind="check-write",
    ),
    # taplo: Rust製TOMLフォーマッター/リンター。shfmtと同様の2段階実行（check → format）。
    # 既定で無効（opt-in）。
    "taplo": CommandInfo(
        type="formatter",
        targets="*.toml",
        fixed_cost=0.3,
        bin_spec=BinToolSpec(bin_name="taplo"),
        defaults={
            # taplo: Rust製TOMLフォーマッター/リンター。bin-runner経由。既定で無効（opt-in）。
            # shfmtと同様の2段階実行（check → format）。
            "taplo": False,
            "taplo-path": "",
            "taplo-runner": "bin-runner",
            "taplo-args": [],
            "taplo-check-args": ["check"],
            "taplo-write-args": ["format"],
            "taplo-version": "latest",
            "taplo-fast": True,
        },
        execution_kind="check-write",
    ),
    # Rust / .NETツール（formatter）。いずれもpass-filenames=FalseでCrate/solution
    # 全体を対象とするproject-level実行で動作する。serial_groupにより同一ツールチェイン
    # （cargo / dotnet）のコマンドは直列実行され、targetディレクトリ等のロック競合を回避する。
    "cargo-fmt": CommandInfo(
        type="formatter",
        targets="*.rs",
        serial_group="cargo",
        fixed_cost=1.0,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            # -- Rust 言語ツール --
            # いずれも pass-filenames=False で crate 全体を対象とする project-level 実行。
            # 既定で bin-runner 経路を通り、グローバル `bin-runner` 既定 (mise) により mise exec で
            # 解決する。従来挙動 (PATH 上の cargo / cargo-deny を直接実行) を維持したい場合は
            # `cargo-fmt-runner = "direct"` 等の明示指定または `cargo-fmt-path` への明示パス指定で切り替えられる。
            "cargo-fmt": False,
            "cargo-fmt-path": "",
            "cargo-fmt-runner": "bin-runner",
            "cargo-fmt-version": "latest",
            # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
            "cargo-fmt-args": ["fmt"],
            "cargo-fmt-pass-filenames": False,
            "cargo-fmt-fast": True,
        },
    ),
    "dotnet-format": CommandInfo(
        type="formatter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props", ".editorconfig"],
        serial_group="dotnet",
        fixed_cost=2.0,
        bin_spec=BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
        language="dotnet",
        defaults={
            # -- .NET 言語ツール --
            # 既定で bin-runner 経路を通り、グローバル `bin-runner` 既定 (mise) により mise exec で
            # 解決する。従来挙動 (PATH 上の dotnet を直接実行) を維持したい場合は
            # `dotnet-format-runner = "direct"` 等の明示指定または `dotnet-format-path` への
            # 明示パス指定で切り替えられる。directモードでは`DOTNET_ROOT`環境変数配下にdotnet実行ファイルが
            # あれば優先採用する。
            "dotnet-format": False,
            "dotnet-format-path": "",
            "dotnet-format-runner": "bin-runner",
            "dotnet-format-version": "latest",
            # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
            "dotnet-format-args": ["format"],
            "dotnet-format-pass-filenames": False,
            "dotnet-format-fast": True,
        },
    ),
    # prekはRust製の`.pre-commit-config.yaml`実行系。workspace rootから再帰的に
    # サブディレクトリの設定ファイルを探索する仕様のため、既定引数へ--configを明示して
    # workspace探索を無効化する（pyfltr/config/config.pyの`prek`設定ブロックのコメント参照）。
    "prek": CommandInfo(
        type="formatter",
        targets="*",
        config_files=[".pre-commit-config.yaml"],
        subproject_aware=False,
        allows_external_paths=False,
        defaults={
            # prek統合。pre-commitと同一の.pre-commit-config.yamlを読むRust製の実行系。
            # 環境変数・段階的実行・出力書式がpre-commitと一致することをprek 0.4.11で実機検証済み。
            # prekはworkspace rootから再帰的にサブディレクトリの設定ファイルを探索して実行対象へ含めるため、
            # 既定引数へ--configを明示してworkspace探索を無効化する（.prekignoreによる除外は
            # 検証バージョンで機能しなかったため採用しない）。
            "prek": False,
            "prek-path": "prek",
            "prek-runner": "direct",
            "prek-args": ["run", "--config=.pre-commit-config.yaml", "--files"],
            "prek-pass-filenames": True,
            "prek-fast": True,
            "prek-auto-skip": True,
            "prek-skip": [],
        },
        execution_kind="precommit",
    ),
    # pre-commitはリポジトリ固有チェックが幅広く実行されるため、他formatterの修正後に最後で呼ぶ。
    # 変更ファイル指定（--files）で起動するが、各hook内部のtypes・types_or・files・excludeフィルタが
    # ファイル指定起動でも適用されるため、関係するhookのみ部分検証できる。
    "pre-commit": CommandInfo(
        type="formatter",
        targets="*",
        config_files=[".pre-commit-config.yaml"],
        subproject_aware=False,
        allows_external_paths=False,
        defaults={
            # pre-commit統合。有効にするとpyfltr run/ci/fast実行時に
            # pre-commit runを変更ファイル指定（--files <対象>）で内部実行する。
            # 各hookの内部フィルタ（types・types_or・files・exclude）はファイル指定起動でも適用されるため、
            # 関係するhookのみ動作する。pass_filenames=Falseのhook（gitleaks等）はpre-commit側で
            # リポジトリ全体走査になるため、ファイル指定渡しでも従来通り動作する。
            # pre-commit-fast = True（既定）によりfastも統合するため、
            # make format相当の場面でpre-commitを別途呼ぶ必要がなくなる。
            # pre-commit配下からpyfltrが起動された場合はPRE_COMMIT=1
            # 環境変数の検出によりpre-commit統合を自動でスキップする。
            # pre-commitとprekを同時に有効化した場合は_warn_precommit_prek_conflictが警告を発行する。
            "pre-commit": False,
            "pre-commit-path": "pre-commit",
            "pre-commit-runner": "direct",
            # `pre-commit run`の位置引数はhook IDとして解釈されるため、
            # ファイル指定には`--files`フラグの前置が必須。
            # また`--files`へ対象ファイルが渡る既定構成では、引数なしの`pre-commit run`が行う
            # 未ステージ変更の退避・復元（`git stash`相当の作業ツリー操作）は発生しない。
            "pre-commit-args": ["run", "--files"],
            "pre-commit-pass-filenames": True,
            "pre-commit-fast": True,
            # .pre-commit-config.yamlからpyfltr関連hookを自動検出してSKIPする
            "pre-commit-auto-skip": True,
            # SKIP環境変数に渡すhook IDの手動指定リスト（auto-skipと併用可能）
            "pre-commit-skip": [],
        },
        execution_kind="precommit",
    ),
    "ec": CommandInfo(
        type="linter",
        targets="*",
        bin_spec=BinToolSpec(
            bin_name="editorconfig-checker",
            mise_backend="github:editorconfig-checker/editorconfig-checker",
        ),
        diagnostic_pattern=r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*\w+:\s*(?P<message>.+)",
        defaults={
            "ec": False,
            "ec-path": "",
            "ec-runner": "bin-runner",
            "ec-args": ["-format", "gcc", "-no-color"],
            "ec-version": "latest",
            "ec-fast": True,
        },
    ),
    "shellcheck": CommandInfo(
        type="linter",
        targets="*.sh",
        per_file_cost=0.03,
        subproject_aware=False,
        bin_spec=BinToolSpec(bin_name="shellcheck"),
        diagnostic_pattern=r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*\w+:\s*(?P<message>.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_shellcheck_json"),
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_shellcheck_url"),
        structured_output=(
            "shellcheck-json",
            StructuredOutputSpec(
                inject=["-f", "json"],
                conflicts=["-f"],
            ),
        ),
        defaults={
            "shellcheck-json": True,
            "shellcheck": False,
            "shellcheck-path": "",
            "shellcheck-runner": "bin-runner",
            "shellcheck-args": ["-f", "gcc"],
            "shellcheck-version": "latest",
            "shellcheck-fast": True,
        },
    ),
    "typos": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=0.04,
        per_file_cost=0.007,
        subproject_aware=False,
        diagnostic_pattern=r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_typos_jsonl"),
        structured_output=(
            "typos-json",
            StructuredOutputSpec(
                inject=["--format=json"],
                conflicts=["--format"],
            ),
        ),
        defaults={
            "typos-json": True,
            "typos": False,
            "typos-path": "",
            "typos-runner": "direct",
            "typos-args": ["--format", "brief"],
            "typos-version": "latest",
            "typos-fast": True,
        },
    ),
    "actionlint": CommandInfo(
        type="linter",
        targets=[".github/workflows/*.yaml", ".github/workflows/*.yml"],
        fixed_cost=0.2,
        bin_spec=BinToolSpec(bin_name="actionlint"),
        diagnostic_pattern=r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+?)(?:\s*\[(?P<rule>[^\[\]\s]+)\])?\s*$",
        defaults={
            "actionlint": False,
            "actionlint-path": "",
            "actionlint-runner": "bin-runner",
            "actionlint-args": [],
            "actionlint-version": "latest",
            "actionlint-fast": True,
        },
    ),
    # pinact: GitHub Actionsの`uses:`がSHAと版コメントでピン留めされているかを検査する。
    # 既定引数の`--no-api`によりGitHub APIを呼ばない構文上の検査に限る。
    "pinact": CommandInfo(
        type="linter",
        targets=[".github/workflows/*.yaml", ".github/workflows/*.yml"],
        fixed_cost=0.1,
        bin_spec=BinToolSpec(bin_name="pinact"),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_pinact_sarif"),
        defaults={
            # pinact: `--no-api`でGitHub APIを呼ばない構文上の検査（40文字SHAと版コメントの有無）に限定し、
            # `GITHUB_TOKEN`を不要にする。`--format sarif`の指定時は`--fix`が既定で無効となりファイルを書き換えない。
            # `--check`は`--no-api`と併用すると終了コード3で失敗するため含めない。
            # 自動修正（タグからSHAへの解決）はGitHub APIを要し結果が実行時点の最新リリースで変わるため、
            # `pinact-fix-args`は定義せずfix段へ載せない。修正は利用者が`pinact run`を直接実行する。
            "pinact": False,
            "pinact-path": "",
            "pinact-runner": "bin-runner",
            "pinact-args": ["run", "--no-api", "--format", "sarif"],
            "pinact-version": "latest",
            "pinact-fast": True,
        },
    ),
    # GitLab CI設定の構文検証。GitLab API経由でlintするためネットワーク・認証が必須。
    # 既定で無効（opt-in）とし、CIや初学者環境で誤って失敗しないようにする。
    "glab-ci-lint": CommandInfo(
        type="linter",
        targets=".gitlab-ci.yml",
        fixed_cost=1.0,
        bin_spec=BinToolSpec(bin_name="glab"),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_glab_ci_lint"),
        defaults={
            # glab ci lint は GitLab API 経由で .gitlab-ci.yml を検証する。
            # 認証・ネットワーク必須のため既定で無効 (opt-in)。
            # サブコマンド `ci lint` は args 既定値として持たせ、明示 path 指定経路でも適用されるようにする。
            "glab-ci-lint": False,
            "glab-ci-lint-path": "",
            "glab-ci-lint-runner": "bin-runner",
            "glab-ci-lint-args": ["ci", "lint"],
            "glab-ci-lint-version": "latest",
            "glab-ci-lint-fast": False,
        },
        execution_kind="glab",
    ),
    # yamllint: Python製YAMLリンター。YAML全般を対象とする。既定で無効（opt-in）。
    "yamllint": CommandInfo(
        type="linter",
        targets=["*.yaml", "*.yml"],
        fixed_cost=0.1,
        per_file_cost=0.01,
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*\[(?P<severity>error|warning)\]"
        r"\s+(?P<message>.+?)(?:\s+\((?P<rule>[^()\s]+)\))?\s*$",
        structured_output=(
            "yamllint-parsable",
            StructuredOutputSpec(
                # 既定の`-f auto`はGitHub Actions上で`::error`形式へ切り替わり、
                # 標準形式は見出し行と字下げした違反行の複数行となるため、
                # 環境によらず1違反1行となる`parsable`へ固定する。
                inject=["-f", "parsable"],
                conflicts=["-f", "--format"],
            ),
        ),
        defaults={
            "yamllint-parsable": True,
            # yamllint: Python製YAMLリンター。既定で無効（opt-in）。直接実行経路。
            "yamllint": False,
            "yamllint-path": "",
            "yamllint-runner": "direct",
            "yamllint-args": [],
            "yamllint-fast": True,
        },
    ),
    # hadolint: Haskell製Dockerfileリンター。bin-runner経由。既定で無効（opt-in）。
    "hadolint": CommandInfo(
        type="linter",
        targets=["Dockerfile", "Dockerfile.*", "*.Dockerfile"],
        fixed_cost=0.2,
        per_file_cost=0.05,
        bin_spec=BinToolSpec(bin_name="hadolint"),
        defaults={
            # hadolint: Dockerfile専用リンター。bin-runner経由。既定で無効（opt-in）。
            "hadolint": False,
            "hadolint-path": "",
            "hadolint-runner": "bin-runner",
            "hadolint-args": [],
            "hadolint-version": "latest",
            "hadolint-fast": True,
        },
    ),
    # gitleaks: Goバイナリ。リポジトリ全体のシークレット検出。
    # pass-filenames=Falseで全体を対象とする。bin-runner経由。既定で無効（opt-in）。
    "gitleaks": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=1.0,
        allows_external_paths=False,
        bin_spec=BinToolSpec(bin_name="gitleaks"),
        defaults={
            # gitleaks: シークレット検出ツール（Goバイナリ）。bin-runner経由。既定で無効（opt-in）。
            # `detect` サブコマンドは args 既定値として持たせる（glab-ci-lint と同じ設計）。
            # pass-filenames=false でリポジトリ全体を対象とする。
            "gitleaks": False,
            "gitleaks-path": "",
            "gitleaks-runner": "bin-runner",
            "gitleaks-args": ["detect", "--no-banner"],
            "gitleaks-pass-filenames": False,
            "gitleaks-version": "latest",
            "gitleaks-fast": False,
        },
    ),
    # semgrep: Python製多言語SASTツール。ルールセット指定が必須のため既定で無効（opt-in）。
    # 利用者が`semgrep-args`で`--config=auto`または個別ルールセットを指定する前提。
    # 対象は多言語のため`*`globで広く対象とし、ツール側のルールセットで実際の検査対象を限定する。
    # `pass-filenames=True`（既定）でファイル一覧を末尾引数として渡す。
    # gitleaksのようなリポジトリ全体走査ではなく対象ファイル群を明示する設計とし、
    # pyfltr側のexclude / .gitignore尊重の効果をそのまま反映する。
    "semgrep": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=2.0,
        per_file_cost=0.05,
        allows_external_paths=False,
        python_bin="semgrep",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_semgrep_json"),
        defaults={
            # semgrep: 多言語SAST。ルールセット指定が必須のため既定で無効（opt-in）。
            # semgrepは`mcp`を厳密ピンし`click`にも強い制約を課すため本体依存から外した。
            # `uvx`既定により別環境で解決し、pyfltrの依存グラフから切り離す。
            # 利用者が版を固定したい場合は`semgrep-path`または`semgrep-runner`で上書きする。
            # 既定argsは空とする（ルールセット既定はsemgrep側の意図と衝突するため）。
            # 利用者は`semgrep-args = ["scan", "--json", "--error", "--config=auto"]`等で
            # サブコマンド・出力形式・ルールセットをまとめて指定する。
            "semgrep": False,
            "semgrep-path": "",
            "semgrep-runner": "uvx",
            "semgrep-args": [],
            "semgrep-fast": False,
        },
    ),
    # bandit: Python専用source-level SAST。既定で無効（opt-in）。
    # YAML/TOML形式設定の自動読み込みはbandit本体にないため`--configfile <設定ファイル>`明示が必須。
    # 起点cwd直下のpyproject.toml / .bandit.yaml / .bandit.tomlを順に探索し、
    # 最初に見つかったファイルを`--configfile <絶対パス>`形式で注入する。
    # `.bandit`はINI形式で`--configfile`の対象外のためbandit本体の`--recursive`時自動探索に委ねる。
    "bandit": CommandInfo(
        type="linter",
        targets="*.py",
        fixed_cost=0.3,
        per_file_cost=0.05,
        config_arg_template=["--configfile", "{path}"],
        config_inject_candidates=[
            "pyproject.toml",
            ".bandit.yaml",
            ".bandit.toml",
        ],
        python_bin="bandit",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_bandit_json"),
        defaults={
            # bandit: Python専用source-level SAST。既定で無効（opt-in）。
            # `--quiet --recursive --format=json`で実行し、JSON出力をパースする。
            # 設定ファイル（pyproject.toml / .bandit.yaml / .bandit.toml）はbandit本体が自動読み込みしないため、
            # CommandInfoのconfig_arg_template経由で`--configfile <絶対パス>`を注入する。
            "bandit": False,
            "bandit-path": "",
            "bandit-runner": "python-runner",
            "bandit-args": ["--quiet", "--recursive", "--format=json"],
            "bandit-fast": False,
        },
    ),
    # Python linter群はモダン順（後ろほど新しい）に並べる。実行順はLPT並列で別管理。
    "pylint": CommandInfo(
        type="linter",
        fixed_cost=1.75,
        per_file_cost=0.3,
        auto_args=[
            ("pylint-pydantic", ["--load-plugins=pylint_pydantic"]),
        ],
        python_bin="pylint",
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[CRWEF]\d+:.+)",
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
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+):\s*error:\s*(?P<message>.+?)(?:\s*\[(?P<rule>[^\]]+)\])?\s*$",
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
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[A-Z]+\d+\s+.+)",
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
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+)\s*-\s*error:\s*(?P<message>.+)",
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
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):"
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
        path_base_parser=FunctionRef[..., list[ErrorLocation]]("pyfltr.parsing.tools", "parse_arid_json"),
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
    "markdownlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=0.9,
        per_file_cost=0.035,
        # 外部パス（起点cwd配下にない絶対パス）を渡すとmarkdownlint-cli2側で
        # 対象探索エラーが未整形のまま出力されるため除外＋警告経路を適用する。
        allows_external_paths=False,
        # `--config`が受け付ける候補（markdownlint-cli2公式README参照）。
        # `config_files`は登録しない（登録すると`warn_config_files`が
        # 設定不在時に警告を発行してしまい、外部パスのみ検査するケースで
        # 「プロジェクト側に設定不要」の運用と矛盾するため）。
        config_arg_template=["--config", "{path}"],
        config_inject_candidates=[
            ".markdownlint-cli2.jsonc",
            ".markdownlint-cli2.yaml",
            ".markdownlint-cli2.cjs",
            ".markdownlint-cli2.mjs",
            ".markdownlint.jsonc",
            ".markdownlint.json",
            ".markdownlint.yaml",
            ".markdownlint.yml",
            ".markdownlint.cjs",
            ".markdownlint.mjs",
        ],
        js_bin="markdownlint-cli2",
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+)(?::(?P<col>\d+))?\s+(?:\w+\s+)?(?P<rule>MD\d+)(?P<message>\S*\s+.+)",
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_markdownlint_url"),
        defaults={
            "markdownlint": False,
            # ユーザーが明示的にpathを設定した場合はその値をそのまま使い、args先頭に自動prefixを追加しない。
            "markdownlint-path": "",
            "markdownlint-args": [],
            "markdownlint-runner": "js-runner",
            "markdownlint-fast": True,
            # fixステージ（pyfltr run / fastの自動修正段）で通常argsの後に追加する引数。
            # markdownlint-cli2は--fixでファイルをin-place修正する。
            "markdownlint-fix-args": ["--fix"],
        },
    ),
    "textlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=2.3,
        per_file_cost=0.4,
        # 外部パス（起点cwd配下にない絶対パス）を渡すとtextlint側で`SearchFilesNoTargetFileError`
        # の未整形スタックトレースが出力されるため除外＋警告経路を適用する。
        allows_external_paths=False,
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
        # `--config`が受け付ける候補（textlint CLI仕様）。
        # `.textlintignore`・`package.json`は`--config`では渡せないため除外する。
        config_arg_template=["--config", "{path}"],
        config_inject_candidates=[
            ".textlintrc",
            ".textlintrc.json",
            ".textlintrc.yml",
            ".textlintrc.yaml",
            ".textlintrc.js",
            ".textlintrc.cjs",
        ],
        cacheable=True,
        js_bin="textlint",
        pnpx_package="textlint@<15.5.3 || >15.5.3",
        diagnostic_pattern=rf"(?P<file>{_FILE}):\s*line\s+(?P<line>\d+),\s*col\s+(?P<col>\d+),\s*\w+\s*-\s*(?P<message>.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_textlint_json"),
        structured_output=(
            "textlint-json",
            StructuredOutputSpec(
                inject=["--format", "json"],
                conflicts=["--format"],
                lint_only=True,
            ),
        ),
        rule_hints={
            "ja-technical-writing/sentence-length": (
                "textlint counts up to the period (。) as one sentence; bullet-line splits still count as one."
                " Split with periods to shorten."
            ),
            "ja-technical-writing/max-ten": (
                "Too many commas (、) in one sentence; split into multiple sentences or revise conjunctions and dependencies."
            ),
            "ja-technical-writing/max-kanji-continuous-len": (
                "Long kanji run detected; insert hiragana, particles, or commas (、) to break it up."
            ),
            "ja-technical-writing/no-unmatched-pair": (
                "Bracket pair is unmatched. Check for typos or missing pairs, and ensure full-width bracket pairs"
                " do not span across line breaks (the rule treats line breaks as separators)."
            ),
        },
        defaults={
            "textlint-json": True,
            "textlint": False,
            "textlint-path": "",
            "textlint-runner": "js-runner",
            # lint / fix共通で常に付与される引数。lint専用オプション（--formatなど）はここではなく
            # textlint-lint-argsに書くこと。fix時は@textlint/fixer-formatterが使用されるが
            # compactフォーマッタが存在しないため、--format compactを共通argsに含めるとfixが失敗する。
            "textlint-args": [],
            # 非fixモード（およびfixモードの後段lintチェック）でのみ付与する引数。
            # 既定はcompactフォーマッタ指定。ただし既定で有効なtextlint-jsonが--format jsonを
            # 注入して既存の--format指定を除去するため、既定構成ではこの値は実効しない。
            "textlint-lint-args": ["--format", "compact"],
            # textlint向けルール / プリセットパッケージの列挙。pnpx / npxモードでは
            # --package / -p展開される。pnpm / npm / yarn / directモードでは
            # package.json側で管理する前提のため無視される。
            "textlint-packages": [
                "textlint-rule-preset-ja-technical-writing",
                "textlint-rule-preset-jtf-style",
                "textlint-rule-ja-no-abusage",
                "textlint-rule-preset-ai-words-ja",
            ],
            "textlint-fast": True,
            # fixモード時に通常argsの後に追加する引数。
            # textlintは--fixでautofix可能なルールをin-place修正する。
            "textlint-fix-args": ["--fix"],
            # fixモード実行で「破損させてはならない識別子」を列挙する。
            # textlint --fixの自動修正がコードブロック外の`.NET` / `Node.js`等の識別子まで
            # 変換してしまうことがあるため、fix前後で識別子が失われたケースを検知して警告を発行する。
            # 空リスト（`[]`）を指定すると検知を無効化できる。
            "textlint-protected-identifiers": [".NET", "Node.js", "Vue.js", "Next.js", "Nuxt.js"],
        },
        fix_execution_kind="textlint-fix",
    ),
    # designmd: @google/design.md による DESIGN.md 形式仕様チェック。
    # 対象ファイル名はDESIGN.md固定（公式仕様）。js-runner経由でnpmパッケージから起動する。
    "designmd": CommandInfo(
        type="linter",
        targets="DESIGN.md",
        fixed_cost=1.5,
        js_bin="design.md",
        pnpx_package="@google/design.md",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_designmd_json"),
        defaults={
            # designmd: @google/design.md による DESIGN.md 形式仕様チェック。js-runner経由。
            # 対象ファイルがあれば自動的に有効化される設計のため既定True。
            "designmd": True,
            "designmd-path": "",
            "designmd-runner": "js-runner",
            # `@google/design.md` の起動形式は `design.md lint <files>`。サブコマンドは共通argsに含める。
            "designmd-args": ["lint"],
            "designmd-fast": False,
        },
    ),
    # lychee: Rust製リンク切れチェッカー。bin-runner経由（mise）で起動する。
    # 外部URLへの到達性検証を主用途とするため、ネットワーク到達性に依存する。
    "lychee": CommandInfo(
        type="linter",
        targets=["*.md", "*.html"],
        fixed_cost=1.0,
        per_file_cost=0.05,
        auto_value_args=[("lychee-max-concurrency", "--max-concurrency={value}")],
        bin_spec=BinToolSpec(bin_name="lychee", mise_backend="github:lycheeverse/lychee"),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_lychee_json"),
        defaults={
            # lychee: Rust製リンク切れチェッカー。bin-runner経由（mise）。既定で有効。
            # 既定argsに`--offline`は加えない（外部URL検証が本来の用途のため）。
            # 5xx・応答タイムアウトだけの失敗は有限再試行後に警告化し、404等は失敗を維持する。
            "lychee": True,
            "lychee-path": "",
            "lychee-runner": "bin-runner",
            "lychee-args": ["--format", "json", "--no-progress"],
            "lychee-version": "latest",
            "lychee-fast": False,
            # lycheeの同時接続数（`--max-concurrency`）。0以下でlychee既定値（128）へ委ねる。
            # GitHub Actions上のlycheeがgithub.com宛リンクに対して
            # `Network error: HTTP/2 protocol error. Server may not support HTTP/2 properly`で
            # 失敗する事象を観測したため、既定値を4へ抑える。
            # 同時接続数が原因であるという一次資料上の裏付けは無いが、
            # 同時接続数を抑えた実行では対象の失敗が再現していないため緩和策として採用する。
            # 利用者側の`{command}-extend-args`での個別対処を不要にする目的でpyfltr既定に置く。
            "lychee-max-concurrency": 4,
        },
    ),
    # colloquial-check: 日本語文書の口語表現を検出する内蔵linter。
    # LLMが出力しがちな口語的な言い回しを`warning`として指摘する（既定で無効・opt-in）。
    # 起動経路は`python -m pyfltr.colloquial`。
    "colloquial-check": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=0.1,
        per_file_cost=0.005,
        subproject_aware=False,
        diagnostic_pattern=rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
        defaults={
            # colloquial-check: 日本語文書の口語表現を検出する内蔵linter。既定で無効（opt-in）。
            # 検出結果はseverity既定"warning"によりCI/pre-commitを失敗させない。
            # `{command}-path`は実行ファイルパス（文字列）を1件のみ許容するため、`python -m`起動は
            # `-path`にインタープリターを、`-args`にモジュール指定を分けて渡す。
            "colloquial-check": False,
            "colloquial-check-path": sys.executable,
            "colloquial-check-args": ["-m", "pyfltr.colloquial"],
            "colloquial-check-runner": "direct",
            "colloquial-check-fast": True,
        },
    ),
    # JS/TS linter群はモダン順（後ろほど新しい）に並べる。
    "tsc": CommandInfo(
        type="linter",
        targets=["*.ts", "*.tsx", "*.mts", "*.cts"],
        js_bin="tsc",
        pnpx_package="typescript",
        language="javascript",
        defaults={
            "tsc": False,
            "tsc-path": "",
            "tsc-runner": "js-runner",
            "tsc-args": ["--noEmit"],
            "tsc-pass-filenames": False,
            "tsc-fast": False,
        },
    ),
    "eslint": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.vue", "*.svelte"],
        fixed_cost=2.3,
        per_file_cost=0.05,
        js_bin="eslint",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_eslint_json"),
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_eslint_url"),
        structured_output=(
            "eslint-json",
            StructuredOutputSpec(
                inject=["--format", "json"],
                conflicts=["--format"],
            ),
        ),
        language="javascript",
        defaults={
            "eslint-json": True,
            "eslint": False,
            "eslint-path": "",
            "eslint-runner": "js-runner",
            # ESLint 9系以降でcompact / unix / tapなどのコアフォーマッタが除去されたため、
            # 構造化出力はeslint-json設定により_STRUCTURED_OUTPUT_SPECS経由で注入する。
            "eslint-args": [],
            "eslint-fast": False,
            # fixモード時に通常argsの後に追加する引数。eslintは--fixでautofixする。
            "eslint-fix-args": ["--fix"],
        },
    ),
    "biome": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.json", "*.jsonc", "*.css"],
        js_bin="biome",
        pnpx_package="@biomejs/biome",
        diagnostic_pattern=r"::(?P<severity>error|warning|notice)\s+(?:[^:]*?title=(?P<rule>[^,]+))?"
        r"[^:]*?file=(?P<file>[^,]+)"
        r"[^:]*?(?<![A-Za-z])line=(?P<line>\d+)"
        r"(?:[^:]*?endLine=(?P<end_line>\d+))?"
        r"[^:]*?(?<![A-Za-z])col=(?P<col>\d+)"
        r"(?:[^:]*?endColumn=(?P<end_col>\d+))?"
        r"[^:]*?::(?P<message>.+)",
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_biome_url"),
        structured_output=(
            "biome-json",
            StructuredOutputSpec(
                inject=["--reporter=github"],
                conflicts=["--reporter"],
            ),
        ),
        language="javascript",
        defaults={
            "biome-json": True,
            "biome": False,
            "biome-path": "",
            "biome-runner": "js-runner",
            # "check"サブコマンドは共通argsに置く。--reporter=githubはbiome-json設定
            # により_STRUCTURED_OUTPUT_SPECS経由で注入する。
            "biome-args": ["check"],
            "biome-fast": True,
            # fixモード時に通常argsの後に追加する引数。
            # ruffの`--unsafe-fixes`採用方針と揃え、safe/unsafe両方のfixを自動適用する。
            # biomeのseverityは各ルールの既定値で決まり、fixのsafe/unsafeとは独立である。
            # severity infoの診断は`::notice`として出力されるが、
            # biome公式設計でinfoは終了コードに影響しない。
            # 個別ルールをCIの失敗対象に変更したい場合は`biome.json`の`linter.rules.*`で
            # severityを上げる（pyfltr側はinfoを失敗扱いに昇格させない）。
            "biome-fix-args": ["--write", "--unsafe"],
        },
    ),
    "oxlint": CommandInfo(
        type="linter",
        targets=[*_JS_COMMON_TARGETS, "*.vue", "*.svelte"],
        fixed_cost=0.7,
        js_bin="oxlint",
        pnpx_package="oxlint",
        language="javascript",
        defaults={
            # -- js-runner対応ツール（追加分） --
            "oxlint": False,
            "oxlint-path": "",
            "oxlint-runner": "js-runner",
            "oxlint-args": [],
            "oxlint-fast": True,
        },
    ),
    # Rust / .NETツール（linter）。pass-filenames=FalseでCrate / solution全体を対象とする。
    "cargo-clippy": CommandInfo(
        type="linter",
        targets=["*.rs", "Cargo.toml"],
        serial_group="cargo",
        fixed_cost=3.0,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            "cargo-clippy": False,
            "cargo-clippy-path": "",
            "cargo-clippy-runner": "bin-runner",
            "cargo-clippy-version": "latest",
            # args は lint / fix 両モードで共通の前半部分。trailing flag (-- -D warnings)
            # は lint-args / fix-args の双方に重複して置き、--fix 時には `--fix` を
            # 中間に挿入できるよう分離している。
            "cargo-clippy-args": ["clippy", "--all-targets"],
            "cargo-clippy-lint-args": ["--", "-D", "warnings"],
            "cargo-clippy-fix-args": ["--fix", "--allow-staged", "--allow-dirty", "--", "-D", "warnings"],
            "cargo-clippy-pass-filenames": False,
            "cargo-clippy-fast": True,
        },
    ),
    "cargo-check": CommandInfo(
        type="linter",
        targets=["*.rs", "Cargo.toml"],
        serial_group="cargo",
        fixed_cost=2.0,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            "cargo-check": False,
            "cargo-check-path": "",
            "cargo-check-runner": "bin-runner",
            "cargo-check-version": "latest",
            "cargo-check-args": ["check", "--all-targets"],
            "cargo-check-pass-filenames": False,
            "cargo-check-fast": False,
        },
    ),
    "cargo-deny": CommandInfo(
        type="linter",
        targets=["Cargo.toml", "Cargo.lock", "deny.toml"],
        serial_group="cargo",
        fixed_cost=1.0,
        bin_spec=BinToolSpec(bin_name="cargo-deny", mise_backend="aqua:EmbarkStudios/cargo-deny"),
        language="rust",
        defaults={
            "cargo-deny": False,
            "cargo-deny-path": "",
            "cargo-deny-runner": "bin-runner",
            "cargo-deny-version": "latest",
            "cargo-deny-args": ["check"],
            "cargo-deny-pass-filenames": False,
            "cargo-deny-fast": False,
        },
    ),
    "dotnet-build": CommandInfo(
        type="linter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
        fixed_cost=5.0,
        bin_spec=BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
        language="dotnet",
        defaults={
            "dotnet-build": False,
            "dotnet-build-path": "",
            "dotnet-build-runner": "bin-runner",
            "dotnet-build-version": "latest",
            "dotnet-build-args": ["build", "--nologo"],
            "dotnet-build-pass-filenames": False,
            "dotnet-build-fast": False,
        },
    ),
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
    # 依存の脆弱性監査ツール群（uv audit / pnpm audit / npm audit / yarn audit）。
    # いずれもパッケージマネージャー自体のサブコマンドを起動する（実体の解決は
    # runner.pyの`PACKAGE_MANAGER_TOOL_BIN`。`{command}-runner = "direct"`既定）。
    # マニフェスト（pyproject.toml / package.json）の存在をトリガーとし、対象ファイルは
    # 渡さず（`{command}-pass-filenames = false`）プロジェクト単位で起動する。
    # 既定で無効（opt-in）。外部脆弱性データベースへの問い合わせで所要時間を見積もれないため
    # fastは無効（`{command}-fast = false`）。`allows_external_paths=False`で
    # リポジトリ外マニフェストを対象外とする。
    "uv-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="pyproject.toml",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="uv",
        minimum_version=(
            (0, 11, 2),
            "0.11.2未満のuvは脆弱性の検出を終了コードへ反映しないため、未検査または未解消の状態が成功として扱われる",
        ),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_uv_audit"),
        defaults={
            # 依存の脆弱性監査ツール群（uv audit / pnpm audit / npm audit / yarn audit）。
            # BUILTIN_COMMANDS登録順・lintエイリアス・order.mdに合わせ、sqlfluffの直後へ配置する。
            # いずれもパッケージマネージャー自体のサブコマンドを直接呼ぶ（`{command}-runner = "direct"`、
            # 実体の解決はrunner.pyの`PACKAGE_MANAGER_TOOL_BIN`）。ネットワーク必須かつ結果が
            # 外部脆弱性データベース更新で変動するため既定で無効（opt-in）・fast無効。
            # `pass-filenames = false`で対象ファイルは渡さずプロジェクト単位で実行する。
            # bin-runner系ではないため`{command}-version`キーは設けない。
            # uv-auditは`--frozen`でロック解決によるuv.lock書き換えを防ぐ（linterは検査のみで副作用を持たない原則）。
            # `uv audit`はuv側の実験的機能であり、既定では実行のたびに実験的機能である旨の警告を標準エラーへ出力する。
            # 警告は監査結果と無関係でCIログの可読性を下げるため、`--preview-features audit`で抑止する。
            # 抑止により実験的機能である旨の注意喚起が利用者の目に触れなくなるため、
            # uv側の仕様変更で監査の挙動が変わる可能性がある点をガイドの版要件記述で補う。
            "uv-audit": False,
            "uv-audit-path": "",
            "uv-audit-runner": "direct",
            "uv-audit-args": ["audit", "--preview-features", "audit", "--frozen", "--no-progress"],
            "uv-audit-pass-filenames": False,
            "uv-audit-fast": False,
        },
    ),
    "pnpm-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="package.json",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="pnpm",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_pnpm_audit_json"),
        defaults={
            "pnpm-audit": False,
            "pnpm-audit-path": "",
            "pnpm-audit-runner": "direct",
            "pnpm-audit-args": ["audit", "--json"],
            "pnpm-audit-pass-filenames": False,
            "pnpm-audit-fast": False,
        },
    ),
    "npm-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="package.json",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="npm",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_npm_audit_json"),
        defaults={
            "npm-audit": False,
            "npm-audit-path": "",
            "npm-audit-runner": "direct",
            "npm-audit-args": ["audit", "--json"],
            "npm-audit-pass-filenames": False,
            "npm-audit-fast": False,
        },
    ),
    "yarn-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="package.json",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="yarn",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_yarn_audit_jsonl"),
        defaults={
            # yarn classic（1.x）はJSON Lines（auditAdvisory / auditSummary行）を出力する。
            # yarn berry（2+）は`yarn npm audit --json`等とサブコマンド体系が異なるため、
            # 利用時は`yarn-audit-args`で上書きする。
            "yarn-audit": False,
            "yarn-audit-path": "",
            "yarn-audit-runner": "direct",
            "yarn-audit-args": ["audit", "--json"],
            "yarn-audit-pass-filenames": False,
            "yarn-audit-fast": False,
        },
    ),
    "pytest": CommandInfo(
        type="tester",
        targets="*_test.py",
        fixed_cost=3.0,
        allows_external_paths=False,
        python_bin="pytest",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.pytest", "parse_pytest"),
        summary_parser=FunctionRef[[str], str | None]("pyfltr.parsing.pytest", "summarize_pytest"),
        structured_output=(
            "pytest-tb-line",
            StructuredOutputSpec(
                # 設定キー名は`--tb=line`を示唆するが、実際に注入するのは`--tb=short`である。
                # `--tb=short`はプロジェクト内フレームを含みつつ出力量を抑えられるため既定採用としており、
                # キー名は初期実装時の名残。設定互換性維持のため改名しない
                inject=["--tb=short"],
                conflicts=["--tb"],
            ),
        ),
        language="python",
        defaults={
            "pytest-tb-line": True,
            "pytest": False,
            "pytest-path": "",
            "pytest-runner": "python-runner",
            "pytest-args": [],
            "pytest-devmode": True,
            "pytest-fast": False,
            # fast選択時だけpytestの対象を置き換えるglob（文字列または配列）。
            # 非空ならpytest-fast=falseでもfastへ参加し、位置引数・差分指定に依らずプロジェクト全域から一致ファイルを選ぶ。
            "pytest-fast-targets": [],
            # 全実行でpytestの対象へ加えるglob（文字列または配列）。
            # 位置引数・差分指定に依らずプロジェクト全域から一致ファイルを選び、通常の対象との和集合を実行する。
            "pytest-always-targets": [],
        },
    ),
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
        allows_external_paths=False,
        js_bin="vitest",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_vitest_json"),
        language="javascript",
        defaults={
            "vitest": False,
            "vitest-path": "",
            "vitest-runner": "js-runner",
            # vitestはrunサブコマンドが必須。また、pyfltrがtargets設定で限定したファイル群と
            # プロジェクト側のvitest include設定が交差せず対象ゼロになるケースでrc=1となり
            # failed扱いになるのを避けるため、--passWithNoTestsを既定に含める。
            "vitest-args": ["run", "--passWithNoTests"],
            "vitest-fast": False,
        },
        execution_kind="vitest",
    ),
    # Rust / .NETツール（tester）。pass-filenames=FalseでCrate / solution全体を対象とする。
    "cargo-test": CommandInfo(
        type="tester",
        targets=["*.rs", "Cargo.toml"],
        serial_group="cargo",
        fixed_cost=3.0,
        allows_external_paths=False,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            "cargo-test": False,
            "cargo-test-path": "",
            "cargo-test-runner": "bin-runner",
            "cargo-test-version": "latest",
            "cargo-test-args": ["test"],
            "cargo-test-pass-filenames": False,
            "cargo-test-fast": False,
        },
    ),
    "dotnet-test": CommandInfo(
        type="tester",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
        fixed_cost=5.0,
        allows_external_paths=False,
        bin_spec=BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
        language="dotnet",
        defaults={
            "dotnet-test": False,
            "dotnet-test-path": "",
            "dotnet-test-runner": "bin-runner",
            "dotnet-test-version": "latest",
            "dotnet-test-args": ["test", "--nologo"],
            "dotnet-test-pass-filenames": False,
            "dotnet-test-fast": False,
        },
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

PYTHON_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "python")
"""python 設定に紐づく Python 系コマンドの一覧。

Python 系ツール一式は本体依存（`dependencies`）に同梱済みで、
`uvx pyfltr` 単発で揃う。`{command}-runner = "python-runner"` 既定（グローバル `python-runner = "uv"` 既定経由）により、
cwdに`uv.lock`がある場合は利用者プロジェクトのuv環境のツール版が優先される。

言語カテゴリキー`python`のgate対象で、preset内でTrueとなっているツールを
通過させる。`python = false`または未指定のときは、preset由来でTrueになった
コマンドも個別`{command} = true`指定がなければFalseに上書きされる。
個別`{command} = true`はgateを越えて優先される。
`ty`のみpreset非収録のため、使用時は個別に`ty = true`を指定する運用を維持する。"""

JAVASCRIPT_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "javascript")
"""javascript 設定に紐づく JavaScript / TypeScript 系コマンドの一覧。

TypeScriptはJavaScriptエコシステム上のツール群（eslint / prettier / tsc等）で
扱うため、専用カテゴリは設けずここに内包する。言語カテゴリキー`javascript`の
gate対象で、preset内でTrueとなっているツールを通過させる。挙動は
`PYTHON_COMMANDS`と同じ。"""

RUST_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "rust")
"""rust 設定に紐づく Rust 系コマンドの一覧。

言語カテゴリキー`rust`のgate対象で、preset内でTrueとなっているツールを
通過させる。挙動は`PYTHON_COMMANDS`と同じ。"""

DOTNET_COMMANDS = tuple(command for command, info in BUILTIN_COMMANDS.items() if info.language == "dotnet")
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
Falseに上書きする）で共通に使う。"""

REMOVED_COMMANDS: frozenset[str] = frozenset({"pyupgrade", "autoflake", "isort", "black", "pflake8"})
"""v3.0.0 で削除されたコマンド名。

設定ファイル中に関連キー（`pyupgrade = true` / `black-args = [...]`など）を検出した
場合、`load_config`が案内付きのValueErrorを送出して移行を促す。"""

AUTO_ARGS = {command: info.auto_args for command, info in BUILTIN_COMMANDS.items() if info.auto_args}
"""コマンドごとの自動引数マッピング。

各タプルは（設定キー, 引数リスト）の対。設定キーがTrueの場合、
引数リストをコマンドライン先頭に自動挿入する。ユーザーの`*-args`と
重複する場合はスキップする。
"""

AUTO_VALUE_ARGS = {command: info.auto_value_args for command, info in BUILTIN_COMMANDS.items() if info.auto_value_args}
"""コマンドごとの数値を伴う自動引数マッピング。

各タプルは（設定キー, 引数テンプレート）の対。設定値が正の整数の場合、
`{value}`を展開した引数をコマンドライン先頭に自動挿入する。
`0`以下はツール側の既定値へ委ねる意味とし、引数を挿入しない。
ユーザーが`*-args`等で同じフラグを指定済みの場合もスキップする。

真偽値の`AUTO_ARGS`と分けるのは、既定値の意味（挿入する/しない）ではなく
数値そのものを利用者が調整する設定だからである。
"""
