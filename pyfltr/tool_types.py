"""ツール定義の型。"""

import dataclasses
import importlib
import typing

from pyfltr.diagnostics import ErrorLocation


@dataclasses.dataclass(frozen=True)
class FunctionRef[**P, R]:
    """登録を読み込む際の循環を避け、関数を呼出時に解決する。"""

    module: str
    name: str

    def resolve(self) -> typing.Callable[P, R]:
        """登録先の関数を型付きで取得する。"""
        return typing.cast(typing.Callable[P, R], getattr(importlib.import_module(self.module), self.name))

    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R:
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
    before_argument: str | None = None
    """指定した引数より前へ注入する（Cargoの`--`とcargo-denyの`check`向け）。"""


ExecutionKind = typing.Literal[
    "plain", "precommit", "glab", "vitest", "textlint-fix", "linter-fix", "ruff-format", "prettier", "check-write"
]

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
    subproject_concurrent: bool = True
    """サブプロジェクト単位の分割実行で、同じツールを複数のサブプロジェクトに対して同時に起動してよいか否か。

    `True`（既定）の場合、`jobs`から導いた件数までサブプロジェクト実行を同時に開始する。
    `False`の場合、`jobs`の値によらずサブプロジェクトを1件ずつ順に実行する。
    同時起動したプロセス間で共有されうる永続状態（キャッシュ、データベース、設定の書込み先）を
    同時に使えないツールで指定する。`mypy`は同じキャッシュ保存先を複数プロセスで同時に使うと
    SQLiteキャッシュの初期化が競合して内部エラーで終了するため`False`とする。

    `serial_group`は異なるツール間の排他であり、本フィールドは同じツールのサブプロジェクト間の直列化を表す。
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
    context_parser: typing.Callable[..., list[ErrorLocation]] | None = None
    """path_base・start_cwdを受け取り、入力ファイルの根拠も検査するparser。"""
    path_resolving_parser: typing.Callable[..., list[ErrorLocation]] | None = None
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
