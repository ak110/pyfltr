"""コマンド実行関連の処理。"""
# pylint: disable=too-many-lines,duplicate-code

import argparse
import atexit
import contextlib
import dataclasses
import hashlib
import json
import logging
import os
import pathlib
import random
import shlex
import shutil
import signal
import subprocess
import threading
import time
import typing

import natsort
import psutil

import pyfltr.config
import pyfltr.error_parser
import pyfltr.paths
import pyfltr.precommit
import pyfltr.warnings_

if typing.TYPE_CHECKING:
    import pyfltr.cache
    import pyfltr.only_failed

logger = logging.getLogger(__name__)


class ProcessRegistry:
    """実行中サブプロセスのスレッドセーフな登録簿。

    グローバル変数による直接管理を本クラスに集約し、テストから差し替え可能な構造にする。
    """

    def __init__(self) -> None:
        # サブプロセスのリストとロック。_active_processes / _active_processes_lockとして
        # モジュール外からも参照できるよう公開属性として定義する。
        self.processes: list[subprocess.Popen[str]] = []
        self.lock = threading.Lock()

    def add(self, proc: "subprocess.Popen[str]") -> None:
        """ロック下でプロセスをリストに追加する。"""
        with self.lock:
            self.processes.append(proc)

    def remove(self, proc: "subprocess.Popen[str]") -> None:
        """ロック下でプロセスをリストから削除する。存在しない場合は無視する。"""
        with self.lock, contextlib.suppress(ValueError):
            self.processes.remove(proc)

    def snapshot(self) -> "list[subprocess.Popen[str]]":
        """ロック下でリストのコピーを返す（terminate_all用）。"""
        with self.lock:
            return list(self.processes)

    def terminate_all(self, *, timeout: float) -> None:
        """全プロセスとその子孫を停止する。

        snapshotを取って各プロセスを `_kill_process_tree` で停止する。
        """
        for proc in self.snapshot():
            with contextlib.suppress(OSError):
                _kill_process_tree(proc, timeout=timeout)

    def cleanup(self) -> None:
        """Atexit用クリーンアップ（タイムアウト1秒で全プロセスを停止）。"""
        self.terminate_all(timeout=1.0)


_DEFAULT_REGISTRY = ProcessRegistry()

# 既存コードおよびテストコードとの互換性のため、ProcessRegistry内部の
# リストとロックをモジュール変数として公開する。
# テストが直接_active_processes.append() / remove() / _active_processes_lockを
# 使う箇所があるため、同一オブジェクトへの参照として維持する。
_active_processes = _DEFAULT_REGISTRY.processes
_active_processes_lock = _DEFAULT_REGISTRY.lock


def set_default_registry(registry: ProcessRegistry) -> None:
    """デフォルトのプロセスレジストリを差し替える（テスト用経路）。

    本Phaseでは既存テストを書き換えないが、今後のテストが独自インスタンスを使いたい
    場合のために用意する。
    """
    global _DEFAULT_REGISTRY, _active_processes, _active_processes_lock  # pylint: disable=global-statement
    _DEFAULT_REGISTRY = registry
    _active_processes = registry.processes
    _active_processes_lock = registry.lock


class InterruptedExecution(Exception):
    """TUIから協調停止が要求されたことを示す例外。

    `_run_subprocess` が `is_interrupted` コールバックで中断指示を検知した際に送出する。
    呼び出し側（`ui._execute_command`）で捕捉し、当該コマンドを `skipped` 結果として置き換える。
    """


# pyfltrのコマンド名 -> 実際に起動するパッケージのbin名の対応表。
# markdownlintコマンドは実体がmarkdownlint-cli2である点に注意。
_JS_TOOL_BIN: dict[str, str] = {
    "textlint": "textlint",
    "markdownlint": "markdownlint-cli2",
    "eslint": "eslint",
    "prettier": "prettier",
    "biome": "biome",
    "vitest": "vitest",
    "oxlint": "oxlint",
    "tsc": "tsc",
}

# pnpx経由で解決するときに `--package` に渡すspec。
# 通常はbin名をそのまま渡すだけだが、上流の既知バグで動かないバージョンを
# 除外したい場合やスコープ付きパッケージの場合にここで差し替える。
# - textlint 15.5.3には起動不能のバグがあるため除外している （15.5.4で修正済み）。
# - biomeはbin名が "biome" だがnpmパッケージは "@biomejs/biome" （スコープ付き）。
_JS_TOOL_PNPX_PACKAGE_SPEC: dict[str, str] = {
    "textlint": "textlint@<15.5.3 || >15.5.3",
    "biome": "@biomejs/biome",
    "oxlint": "oxlint",
    "tsc": "typescript",  # tscコマンドはtypescriptパッケージに含まれる
}


@dataclasses.dataclass(frozen=True)
class BinToolSpec:
    """bin-runner対応ツールの解決情報。"""

    bin_name: str
    """実行ファイル名"""
    mise_backend: str | None = None
    """mise exec用のbackend指定（省略時はbin_name）"""
    default_version: str = "latest"
    """既定バージョン"""


# bin-runnerで解決するネイティブバイナリツールの定義。
# `{command}-runner` が "bin-runner"（グローバル `bin-runner` へ委譲）または "mise" のとき、
# このテーブルからmise backendとbin名を引いてコマンドラインを組み立てる。
# `{command}-path` が非空ならその値を優先し本テーブルは参照しない。
_BIN_TOOL_SPEC: dict[str, BinToolSpec] = {
    "ec": BinToolSpec(bin_name="ec", mise_backend="editorconfig-checker"),
    "shellcheck": BinToolSpec(bin_name="shellcheck"),
    "shfmt": BinToolSpec(bin_name="shfmt"),
    "actionlint": BinToolSpec(bin_name="actionlint"),
    # glab本体は単一バイナリで `glab ci lint` のサブコマンドを必要とするが、
    # サブコマンド注入は-args既定値 （["ci", "lint"]） 側に持たせて、
    # bin-runnerを経由しない明示path指定でも自然にサブコマンドが付く設計とする。
    "glab-ci-lint": BinToolSpec(bin_name="glab"),
    "taplo": BinToolSpec(bin_name="taplo"),
    "hadolint": BinToolSpec(bin_name="hadolint"),
    # gitleaksは `detect` サブコマンドが必須だが、サブコマンド注入は
    # -args既定値側に持たせる（glab-ci-lintと同じ設計）。
    "gitleaks": BinToolSpec(bin_name="gitleaks"),
    # cargo系は `cargo` バイナリを呼ぶ。miseのrust toolchain backendで解決し、
    # cargo-fmt / cargo-clippy / cargo-check / cargo-testはサブコマンドを `-args`
    # 既定値側に持たせる設計とする。
    "cargo-fmt": BinToolSpec(bin_name="cargo", mise_backend="rust"),
    "cargo-clippy": BinToolSpec(bin_name="cargo", mise_backend="rust"),
    "cargo-check": BinToolSpec(bin_name="cargo", mise_backend="rust"),
    "cargo-test": BinToolSpec(bin_name="cargo", mise_backend="rust"),
    # cargo-denyは単独バイナリ。mise registryから消失したためaquaレジストリ経由を既定とする。
    # 利用者がregistry経由などへ切り替えたい場合は `cargo-deny-version` に
    # `cargo-deny@latest` のように `:` または `@` を含む値を渡せばtool spec全体として扱う
    # （build_commandline側の分岐を参照）。
    "cargo-deny": BinToolSpec(bin_name="cargo-deny", mise_backend="aqua:EmbarkStudios/cargo-deny"),
    # dotnet系はいずれも `dotnet` バイナリを呼ぶ。miseのdotnet backendで解決する。
    "dotnet-format": BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
    "dotnet-build": BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
    "dotnet-test": BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
}


@dataclasses.dataclass(frozen=True)
class _StructuredOutputSpec:
    """構造化出力用の引数注入仕様。

    `-args` とは独立した経路で出力形式引数を強制注入する。
    注入時はcommandlineからconflictsに一致する既存引数を除去したうえで
    injectを追加する（ruff/typosは重複指定でエラーになるため）。
    """

    inject: list[str]
    """注入する引数"""
    conflicts: list[str]
    """commandlineから除去する引数プレフィクス"""
    lint_only: bool = False
    """Trueのときfixモードでは注入しない"""


# 各ツールの構造化出力用引数。設定キー → 注入仕様のマッピング。
# 設定キー（例: "ruff-check-json"）がTrueのとき有効になる。
_STRUCTURED_OUTPUT_SPECS: dict[str, tuple[str, _StructuredOutputSpec]] = {
    "ruff-check-json": (
        "ruff-check",
        _StructuredOutputSpec(
            inject=["--output-format=json"],
            conflicts=["--output-format"],
        ),
    ),
    "pylint-json": (
        "pylint",
        _StructuredOutputSpec(
            inject=["--output-format=json2"],
            conflicts=["--output-format"],
        ),
    ),
    "pyright-json": (
        "pyright",
        _StructuredOutputSpec(
            inject=["--outputjson"],
            conflicts=["--outputjson"],
        ),
    ),
    "pytest-tb-line": (
        "pytest",
        _StructuredOutputSpec(
            inject=["--tb=short"],
            conflicts=["--tb"],
        ),
    ),
    "shellcheck-json": (
        "shellcheck",
        _StructuredOutputSpec(
            inject=["-f", "json"],
            conflicts=["-f"],
        ),
    ),
    "textlint-json": (
        "textlint",
        _StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
            lint_only=True,
        ),
    ),
    "typos-json": (
        "typos",
        _StructuredOutputSpec(
            inject=["--format=json"],
            conflicts=["--format"],
        ),
    ),
    "eslint-json": (
        "eslint",
        _StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
        ),
    ),
    "biome-json": (
        "biome",
        _StructuredOutputSpec(
            inject=["--reporter=github"],
            conflicts=["--reporter"],
        ),
    ),
}


def _get_structured_output_spec(command: str, config: pyfltr.config.Config) -> _StructuredOutputSpec | None:
    """コマンドに対応する構造化出力仕様を返す。無効化されていればNone。"""
    for config_key, entry in _STRUCTURED_OUTPUT_SPECS.items():
        cmd = entry[0]
        spec = entry[1]
        if cmd == command and config.values.get(config_key, False):
            return spec
    return None


def _apply_structured_output(commandline: list[str], spec: _StructuredOutputSpec) -> list[str]:
    """Commandlineから衝突する引数を除去し、構造化出力引数を注入する。"""
    filtered: list[str] = []
    skip_next = False
    for i, arg in enumerate(commandline):
        if skip_next:
            skip_next = False
            continue
        matched = False
        for prefix in spec.conflicts:
            if arg == prefix:
                # "-f gcc" 形式: 次の引数もスキップ
                if i + 1 < len(commandline) and not commandline[i + 1].startswith("-"):
                    skip_next = True
                matched = True
                break
            if arg.startswith(f"{prefix}=") or (arg.startswith(prefix) and arg != prefix):
                # "--format=json" 形式 / "--outputjson" 形式
                matched = True
                break
        if not matched:
            filtered.append(arg)
    return [*filtered, *spec.inject]


@dataclasses.dataclass(frozen=True)
class ResolvedCommandline:
    """コマンドライン解決結果（副作用なし）。

    `executable` と `prefix` は `[executable, *prefix]` で起動するための値。
    `runner` は経緯を表現する文字列で `command-info` サブコマンドや診断で使う。
    `runner_source` は `runner` の決定経緯を示す
    （`"explicit"` : `{command}-runner` 明示、`"default"` : 既定値、
    `"path-override"` : `{command}-path` 非空でdirect強制）。
    `effective_runner` はグローバル設定への委譲を解決した最終形
    （`"direct"` / `"mise"` / `"js-pnpx"` / `"js-pnpm"` 等）。
    `tool_spec_omitted` はmise経路で `["exec", "--", <bin>]` 形（tool spec省略形）を採用したかを示す。
    commandline文字列の見た目に頼らず判別できるよう、command-info等で明示露出する用途で持つ。
    """

    executable: str
    prefix: list[str]
    runner: str
    runner_source: str
    effective_runner: str
    tool_spec_omitted: bool = False

    @property
    def commandline(self) -> list[str]:
        """`[executable, *prefix]` の単一リスト表現を返す。"""
        return [self.executable, *self.prefix]


def resolve_runner(command: str, config: pyfltr.config.Config) -> tuple[str, str]:
    """`{command}-runner` 設定値とその決定経緯を返す。

    返り値は `(runner, source)` で、`source` は次のいずれか。

    - `"explicit"`: pyproject.tomlで `{command}-runner` を明示指定
    - `"default"`: 設定既定値（typos等で `"direct"` 等）

    pyproject.toml由来か既定値かを区別するために `Config.values` のフラグでは
    検出できないため、現状は `DEFAULT_CONFIG` との同一性で判定する近似を使う。
    """
    runner = config.values.get(f"{command}-runner")
    if runner is None:
        # 既定値が登録されていないコマンド（カスタムコマンド等）はdirect扱い。
        return "direct", "default"
    default_runner = pyfltr.config.DEFAULT_CONFIG.get(f"{command}-runner")
    source = "default" if runner == default_runner else "explicit"
    return str(runner), source


# `mise ls --current --json` 取得結果の状態スラッグ。
# - `ok`: 取得成功（dictは空でも`mise.toml`記述が無いだけの場合は`ok`）
# - `mise-not-found`: `mise`がPATH上に存在しない
# - `untrusted-no-side-effects`: 未信頼config由来エラーで副作用OFFのためtrust試行を行わなかった
# - `trust-failed`: trust試行が拒否された
# - `exec-error`: その他のmise実行エラー（OSError含む）
# - `json-parse-error`: stdoutのJSONパースに失敗した
# - `unexpected-shape`: dict以外の値が返された
MiseActiveToolsStatus = typing.Literal[
    "ok",
    "mise-not-found",
    "untrusted-no-side-effects",
    "trust-failed",
    "exec-error",
    "json-parse-error",
    "unexpected-shape",
]


@dataclasses.dataclass(frozen=True)
class MiseActiveToolsResult:
    """`mise ls --current --json` 取得結果のステータス付き構造体。

    取得成功時は `tools` がmise本体の解決結果（プロジェクト `mise.toml` ＋グローバル設定の合算）。
    取得失敗時は `tools` を空辞書とし、`detail` に短い手がかり（mise stderr冒頭等）を入れる。
    `command-info` 出力やJSONL header経由で利用者に状況を見える化する目的で使う。
    """

    status: MiseActiveToolsStatus
    tools: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    detail: str | None = None


# `mise ls --current --json` 結果のプロセス内キャッシュ。
# キーは `(realpath(cwd), env_signature, allow_side_effects)` のタプルで、
# - `realpath(cwd)`: pyfltrは同一プロセス内で `os.chdir()` を伴う複数回 `run()` 呼び出しに
#   対応するため、cwd差分で別エントリとして扱う必要がある。
# - `env_signature`: mise設定解決に影響する環境変数（`MISE_CONFIG_FILE` 等）が変化した場合に
#   別キャッシュを参照させる。値が無いキーは固定sentinel `None` でキー化する。
# - `allow_side_effects`: 副作用OFFで未信頼や失敗でフォールバックしたエントリと、副作用ONで
#   trust経由で正規取得したエントリを共存させるため、フラグ自体もキーに含める。
#   これにより `command-info --check` 無し → 有り の順で呼ばれた際に副作用OFFで保存した
#   フォールバック結果が後続を阻害せず、副作用ON呼び出しで正規化される流れを担保する。
_MISE_ACTIVE_TOOLS_CACHE: dict[tuple[str, tuple[tuple[str, str | None], ...], bool], MiseActiveToolsResult] = {}

# mise設定解決に影響する環境変数。値の差分で別キャッシュエントリとして扱う。
# `MISE_CONFIG_FILE`: 利用するconfigファイルの明示指定。
# `MISE_ENV`: 環境別configプロファイル（`mise.{env}.toml`）の選択。
# `MISE_DEFAULT_CONFIG_FILENAME`: 既定configファイル名の上書き。
_MISE_CONFIG_ENV_KEYS: tuple[str, ...] = (
    "MISE_CONFIG_FILE",
    "MISE_ENV",
    "MISE_DEFAULT_CONFIG_FILENAME",
)


def _mise_env_signature() -> tuple[tuple[str, str | None], ...]:
    """mise設定解決に影響する環境変数の現在値を組にして返す。

    値が未設定のキーは `None` をsentinelとして使い、未設定状態と空文字を区別する。
    キャッシュキー要素として使うためタプルで返す。
    """
    return tuple((key, os.environ.get(key)) for key in _MISE_CONFIG_ENV_KEYS)


def _get_mise_active_tools(
    config: pyfltr.config.Config,
    *,
    allow_side_effects: bool = False,
) -> MiseActiveToolsResult:
    """`mise ls --current --json` 結果をステータス付きで返す（プロセス内キャッシュ付き）。

    `tools` はmiseが解決した活性化ツール一覧（プロジェクト `mise.toml` ＋グローバル設定の合算）。
    キーは `mise.toml` 記述そのままの形（例: `rust`、`aqua:EmbarkStudios/cargo-deny`、
    `actionlint`）で、値はmise本体が返すツール情報の配列。

    `allow_side_effects=True` 時は未信頼config由来エラーで `config["mise-auto-trust"]` が
    真なら `mise trust --yes --all` を1回試行し、成功時に `mise ls` を再実行する。
    `allow_side_effects=False` 時は未信頼エラー・mise不在・JSON parse失敗・その他失敗で
    `tools` を空辞書のままフォールバックし、ステータス文字列で取得状況を表現する
    （`command-info` の `--check` 無し呼び出しで副作用なし契約を維持するため）。

    キャッシュキーには `realpath(cwd)`・mise設定解決に影響する環境変数・副作用許可フラグの
    3要素を含める。`mise ls --current --json` がcwdで結果を変えるため、`os.chdir()` 後の
    再呼び出しで誤判定しないよう必ずcwdをキーに含める。副作用OFFで失敗フォールバック保存した
    結果と副作用ONで正規取得した結果が混線しないよう、フラグ自体もキーに含める。
    """
    cache_key = (os.path.realpath(os.getcwd()), _mise_env_signature(), allow_side_effects)
    cached = _MISE_ACTIVE_TOOLS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    result = _query_mise_active_tools(config, allow_side_effects=allow_side_effects)
    _MISE_ACTIVE_TOOLS_CACHE[cache_key] = result
    return result


def _query_mise_active_tools(
    config: pyfltr.config.Config,
    *,
    allow_side_effects: bool,
) -> MiseActiveToolsResult:
    """`mise ls --current --json` を実際に呼び出し、ステータス付き結果を返す。

    キャッシュ管理は呼び出し側 `_get_mise_active_tools` が担当する純取得層。
    取得失敗時のフォールバック挙動は本関数docstringおよび `_get_mise_active_tools` の
    `allow_side_effects` 説明と同義。
    """
    if shutil.which("mise") is None:
        return MiseActiveToolsResult(status="mise-not-found")
    mise_env = _build_mise_subprocess_env(dict(os.environ))
    ls_args = ["mise", "ls", "--current", "--json"]
    return _run_mise_ls_with_trust_retry(ls_args, config, mise_env, allow_side_effects=allow_side_effects)


def _run_mise_with_trust(
    args: list[str],
    mise_env: dict[str, str],
    config: pyfltr.config.Config,
    *,
    allow_side_effects: bool,
) -> tuple[int, str, str, bool]:
    """miseコマンドを実行し、未信頼エラー時はtrust試行→再実行する核ロジック。

    戻り値は `(returncode, stdout, stderr, trust_failed)` のタプル。
    `trust_failed=True` はtrust試行自体が失敗したことを示し、
    呼び出し側でエラーメッセージを出し分けるために使う。
    `OSError` は呼び出し側へ伝播する。
    `allow_side_effects=False` 時はtrust試行を行わず、未信頼エラーも含めてそのまま返す。
    """
    trusted = False
    while True:
        check = subprocess.run(args, capture_output=True, text=True, check=False, env=mise_env)
        if check.returncode == 0:
            return check.returncode, check.stdout, check.stderr, False
        stderr = check.stderr
        if not trusted and allow_side_effects and config["mise-auto-trust"] and "not trusted" in stderr:
            trust = subprocess.run(
                ["mise", "trust", "--yes", "--all"],
                capture_output=True,
                text=True,
                check=False,
                env=mise_env,
            )
            if trust.returncode == 0:
                trusted = True
                continue
            return trust.returncode, "", trust.stderr, True
        # 未信頼以外のエラー、副作用OFF下の未信頼、trust試行後の再失敗はすべてそのまま返す。
        return check.returncode, check.stdout, check.stderr, False


def _run_mise_ls_with_trust_retry(
    ls_args: list[str],
    config: pyfltr.config.Config,
    mise_env: dict[str, str],
    *,
    allow_side_effects: bool,
) -> MiseActiveToolsResult:
    """`mise ls --current --json` を実行し、必要に応じてtrust試行→再実行する。

    成功時は `MiseActiveToolsResult(status="ok", tools=...)` を返す。
    失敗時（mise呼び出し失敗・JSONパース失敗・副作用OFF下の未信頼エラー・trust拒否）は
    対応するステータスを設定したMiseActiveToolsResultを返してフォールバックさせる。
    trust試行を含むリトライ核ロジックは `_run_mise_with_trust` に委譲する。
    """
    try:
        returncode, stdout, stderr, trust_failed = _run_mise_with_trust(
            ls_args, mise_env, config, allow_side_effects=allow_side_effects
        )
    except OSError as e:
        # mise自体の起動失敗（PATH不一致・実行権限なしなど）。判定不可として空扱い。
        return MiseActiveToolsResult(status="exec-error", detail=_summarize_mise_detail(str(e)))
    if returncode != 0:
        if trust_failed:
            return MiseActiveToolsResult(status="trust-failed", detail=_summarize_mise_detail(stderr))
        if not allow_side_effects and "not trusted" in stderr:
            return MiseActiveToolsResult(status="untrusted-no-side-effects", detail=_summarize_mise_detail(stderr))
        return MiseActiveToolsResult(status="exec-error", detail=_summarize_mise_detail(stderr))
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as e:
        return MiseActiveToolsResult(status="json-parse-error", detail=_summarize_mise_detail(str(e)))
    if not isinstance(parsed, dict):
        # mise本体は通常dictを返すが、形式が想定外（list等）なら判定対象外として空扱いにする。
        return MiseActiveToolsResult(status="unexpected-shape", detail=f"got {type(parsed).__name__}")
    return MiseActiveToolsResult(status="ok", tools=parsed)


def _summarize_mise_detail(text: str, *, max_len: int = 200) -> str | None:
    """mise実行失敗時のstderr/exception文字列を短い1行手がかりへ整形する。

    複数行はスペースへ畳み、空白を圧縮した上で先頭`max_len`文字に切り詰める。
    JSONLや`command-info`出力の付帯情報として人間・LLM双方が読みやすい長さに揃えるため。
    空または整形後に空となる場合は`None`を返す。
    """
    flat = " ".join(text.split())
    if not flat:
        return None
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1].rstrip() + "…"


def get_mise_active_tool_key(command: str) -> str | None:
    """`command`がmise active tools辞書を引く際の照合キーを返す。

    判定キーは `_BIN_TOOL_SPEC[command]` の `mise_backend or bin_name` で、
    mise.toml記述（例: `rust`、`aqua:EmbarkStudios/cargo-deny`、`actionlint`）に対応する。
    miseバックエンド未登録のコマンド（python系・js系）は `None` を返す。
    `command-info` で名称ずれの自己診断に使う。
    """
    spec = _BIN_TOOL_SPEC.get(command)
    if spec is None:
        return None
    return spec.mise_backend or spec.bin_name


def _is_tool_active_in_mise_config(
    command: str,
    spec: BinToolSpec,
    config: pyfltr.config.Config,
    *,
    allow_side_effects: bool,
) -> bool:
    """mise設定で当該ツールが活性化されているかを判定する。

    判定キーは `spec.mise_backend or spec.bin_name`（mise.toml記述に合わせた形）。
    例えばcargo系なら `rust`、cargo-denyなら `aqua:EmbarkStudios/cargo-deny`、
    その他のシンプル系（actionlint等）は `bin_name` でそのまま引く。

    `_get_mise_active_tools` のキャッシュ・フォールバック挙動を利用するため、
    取得失敗時は自然に `False`（記述なし扱い）が返り、tool spec省略を発動しない。
    """
    del command  # 現状判定にコマンド名は使わない（specキーで一意）。引数は将来拡張余地のため残す。
    result = _get_mise_active_tools(config, allow_side_effects=allow_side_effects)
    key = spec.mise_backend or spec.bin_name
    return key in result.tools


def build_commandline(
    command: str,
    config: pyfltr.config.Config,
    *,
    allow_side_effects: bool = False,
) -> ResolvedCommandline:
    """ツール起動コマンドラインを構築する（副作用は `allow_side_effects` で制御）。

    `{command}-runner` および `{command}-path` の設定に従い、`mise exec ... --` 形式・
    `pnpx --package ...` 形式・直接実行（PATH解決）のいずれかを返す。
    mise経路では `_get_mise_active_tools` を引いて、mise設定（プロジェクト `mise.toml` ＋
    グローバル設定）に該当ツール記述があり、かつ `{command}-version` が既定値 `"latest"` の
    ときに限りtool spec部分を省略した `["exec", "--", <bin>]` 形を返す
    （miseがmise設定の解決済み内容、つまりcomponentsや固定バージョンをそのまま使えるようにするため）。

    `allow_side_effects=False`（既定）では `mise exec --version` の事前チェックや
    `mise trust` を行わない。判定関数 `_get_mise_active_tools` も副作用OFFで呼び、
    未信頼config由来エラーを「記述なし」扱いとして従来形のtool spec組み立てへフォールバックする。
    `command-info` サブコマンドの `--check` 無し呼び出しから安全に呼べるようにするためである。
    `allow_side_effects=True` 時は判定経路でも `mise-auto-trust` 設定に従いtrust→再呼び出しを許可する。

    ツールが特定できない場合は `FileNotFoundError` を、
    `{command}-runner` 値の組み合わせ自体が不正な場合は `ValueError` を送出する。
    """
    runner, source = resolve_runner(command, config)
    if runner == "bin-runner":
        effective = config["bin-runner"]
    elif runner == "js-runner":
        effective = f"js-{config['js-runner']}"
    elif runner in ("mise", "direct"):
        effective = runner
    else:
        raise ValueError(f"{command}-runnerの設定値が正しくありません: {runner=}")

    # 明示的にmise / js-runnerを指定したのにbackend / binが未登録の場合は、
    # 経緯（path上書きの有無）に関係なくエラーとする（ユーザー意図を尊重するため）。
    if runner == "mise" and command not in _BIN_TOOL_SPEC:
        raise ValueError(f'{command}: mise backend が登録されていないため `{command}-runner = "mise"` は指定できません')
    if runner == "js-runner" and command not in _JS_TOOL_BIN:
        raise ValueError(f'{command}: js-runner 対応ツールではないため `{command}-runner = "js-runner"` は指定できません')

    # `{command}-path` が非空ならば、その値でdirect実行する（明示パス上書き）。
    # 上のバリデーションで明示runnerと未登録の組み合わせは弾いているため、ここに到達するのは
    # 実行自体が成立する組み合わせのみ。
    if config.values.get(f"{command}-path", "") != "":
        return ResolvedCommandline(
            executable=config[f"{command}-path"],
            prefix=[],
            runner=runner,
            runner_source="path-override",
            effective_runner="direct",
        )

    if effective == "mise":
        if command not in _BIN_TOOL_SPEC:
            raise ValueError(f'{command}: mise backend が登録されていないため `{command}-runner = "mise"` は指定できません')
        spec = _BIN_TOOL_SPEC[command]
        version = config.values.get(f"{command}-version", spec.default_version)
        tool_spec_omitted = False
        # version値に `:`（backend prefix区切り）または `@`（tool@version区切り）を含む場合は
        # miseのtool spec全体として扱い、bin_name接頭辞や既定backendを付け足さない。
        # これにより `aqua:Org/Repo@x` のような任意backend指定や、既定backendを上書きする
        # `cargo-deny@latest` のようなregistry経由維持指定をpyfltr設定だけで表現できる。
        if ":" in version or "@" in version:
            prefix = ["exec", version, "--", spec.bin_name]
        elif version == spec.default_version and _is_tool_active_in_mise_config(
            command, spec, config, allow_side_effects=allow_side_effects
        ):
            # mise設定に当該ツール記述があり、かつversionが既定値（"latest"）の場合のみtool specを省略する。
            # これによりmise本体がmise設定の解決済み内容（componentsや固定バージョン）をそのまま使い、
            # pyfltrとmise設定の二重管理を回避する。
            # version明示時（"latest"以外）は利用者の意図を尊重して従来通りtool spec組み立てに留める。
            prefix = ["exec", "--", spec.bin_name]
            tool_spec_omitted = True
        else:
            tool_name = spec.mise_backend or spec.bin_name
            prefix = ["exec", f"{tool_name}@{version}", "--", spec.bin_name]
        return ResolvedCommandline(
            executable="mise",
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=effective,
            tool_spec_omitted=tool_spec_omitted,
        )

    if effective.startswith("js-"):
        if command not in _JS_TOOL_BIN:
            raise ValueError(f'{command}: js-runner 対応ツールではないため `{command}-runner = "js-runner"` は指定できません')
        executable, prefix = _resolve_js_commandline(command, config)
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )

    # effective == "direct"
    if command in _BIN_TOOL_SPEC:
        spec = _BIN_TOOL_SPEC[command]
        executable = _resolve_direct_executable(spec.bin_name)
        return ResolvedCommandline(
            executable=executable,
            prefix=[],
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    if command in _JS_TOOL_BIN:
        # JSツールのdirectはnode_modules/.bin/<cmd> 解決に委譲。
        executable, prefix = _resolve_js_commandline(command, config)
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    # bin/jsのいずれにも未登録でpathも空 → 解決不能。
    raise FileNotFoundError(
        f"{command}: 実行ファイルが特定できません ({command}-path もしくは {command}-runner を設定してください)"
    )


def _resolve_direct_executable(bin_name: str) -> str:
    """Directモードでの実行ファイル解決。

    `dotnet` の場合は `DOTNET_ROOT` 環境変数配下に存在すれば優先する。
    PATH上に存在すれば `shutil.which` で絶対パスへ解決し、見つからなければ
    `FileNotFoundError` を送出する。
    """
    if bin_name == "dotnet":
        dotnet_root = os.environ.get("DOTNET_ROOT")
        if dotnet_root:
            for candidate_name in ("dotnet.exe", "dotnet") if os.name == "nt" else ("dotnet",):
                candidate = pathlib.Path(dotnet_root) / candidate_name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    resolved = shutil.which(bin_name)
    if resolved is None:
        raise FileNotFoundError(bin_name)
    return resolved


def _resolve_bin_commandline(
    command: str,
    config: pyfltr.config.Config,
) -> tuple[str, list[str]]:
    """旧API互換の薄いwrapper（既存テスト・後方互換用）。

    内部的には `build_commandline` と `ensure_mise_available` を組み合わせて
    `(executable, prefix)` を返す。新規利用箇所では `build_commandline` を直接使う。
    本wrapperは `ensure_mise_available` を必ず呼ぶ副作用ありの経路であるため、
    `build_commandline` 側にも `allow_side_effects=True` を渡してmise設定判定の
    trustリトライを許可し、両者の副作用契約を揃える。
    """
    resolved = build_commandline(command, config, allow_side_effects=True)
    resolved = ensure_mise_available(resolved, config, command=command)
    return resolved.executable, list(resolved.prefix)


def ensure_mise_available(
    resolved: ResolvedCommandline,
    config: pyfltr.config.Config,
    *,
    command: str | None = None,
) -> ResolvedCommandline:
    """Mise経由実行時に `mise exec --version` の事前チェックを行う（副作用あり）。

    miseバイナリ自体がPATHに存在しない場合はdirect解決へフォールバックする
    （ディストロ標準パッケージだけで導入された環境でも動かすための救済挙動）。
    config未信頼が検出された場合は `mise-auto-trust` 設定に従い `mise trust --yes --all`
    を1回だけ試行する。失敗時は `FileNotFoundError` を送出する。
    direct実行やjs-runner実行の場合は本関数を素通りで返す。

    `command` にはpyfltrのコマンド名（`cargo-deny` 等）を渡す。
    解決失敗時のエラー文面で `{command}-runner = "direct"` への切替案内に用いる。
    省略時は `bin_name` を案内に流用するが、cargo系のように複数コマンドが
    同じ `bin_name` を共有する場合は誤った案内になるため、極力指定する。
    """
    if resolved.executable != "mise":
        return resolved
    # `build_commandline` の `effective == "mise"` 分岐は次の2形態を返す。
    # - tool spec省略形: `prefix = ["exec", "--", <bin>]`（mise設定記述あり時）
    # - 従来形: `prefix = ["exec", <tool_spec>, "--", <bin>]`（mise設定記述なし時）
    # `prefix[1] == "--"` で両形態を判別し、`mise exec --version` 用argsとエラー文面を出し分ける。
    bin_name = resolved.prefix[-1]
    has_tool_spec = len(resolved.prefix) >= 2 and resolved.prefix[1] != "--"
    tool_spec = resolved.prefix[1] if has_tool_spec else ""

    if shutil.which("mise") is None:
        # mise不在 → direct PATH解決へフォールバック。
        resolved_path = shutil.which(bin_name)
        if resolved_path is None:
            raise FileNotFoundError(bin_name)
        return dataclasses.replace(resolved, executable=resolved_path, prefix=[], effective_runner="direct")

    # mise経由の事前チェック・trust呼び出しでもmiseがtoolパスにフォールバック
    # 解決してしまう挙動を回避するため、PATHからmise toolパスを除外したenvを渡す。
    # 詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
    mise_env = _build_mise_subprocess_env(dict(os.environ))
    if has_tool_spec:
        check_args = ["mise", "exec", tool_spec, "--", bin_name, "--version"]
    else:
        check_args = ["mise", "exec", "--", bin_name, "--version"]
    returncode, _stdout, stderr, trust_failed = _run_mise_with_trust(check_args, mise_env, config, allow_side_effects=True)
    if returncode == 0:
        return resolved
    if trust_failed:
        raise FileNotFoundError(f"mise trust --yes --all: {stderr.strip()}")
    # mise registryからのツール消失・バージョン解決失敗などで起動できない場合に、
    # 利用者がpyfltr設定だけで救済できるようdirect経路への切替を案内する。
    hint_command = command if command is not None else bin_name
    hint = f'{hint_command}-runner = "direct" を指定するとmiseを介さずPATH上のバイナリを直接実行します'
    prefix_text = f"mise exec {tool_spec} -- {bin_name}" if has_tool_spec else f"mise exec -- {bin_name}"
    raise FileNotFoundError(f"{prefix_text}: {stderr.strip()}\n{hint}")


def _failed_resolution_result(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    message: str,
    *,
    files: int,
) -> "CommandResult":
    """ツール解決失敗時の `CommandResult` を組み立てる。

    `files` には実際の処理対象件数を渡す。`status` は `resolution_failed` を返し、
    通常の実行失敗（`failed`）と区別できるようにする。
    """
    pyfltr.warnings_.emit_warning(source="tool-resolve", message=f"{command}: {message}")
    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=[],
        returncode=1,
        has_error=True,
        files=files,
        output=message,
        elapsed=0.0,
        resolution_failed=True,
    )


def _kill_process_tree(proc: "subprocess.Popen[str]", *, timeout: float) -> None:
    """Procとその子孫をまとめて停止する。

    `_run_subprocess` はPOSIXでは `start_new_session=True`、Windowsでは
    `CREATE_NEW_PROCESS_GROUP` でPopenを起動している。pytest-xdistのように
    サブプロセスが更にサブプロセスをforkしてパイプを継承するツールでは、
    親だけ `terminate()` しても孫がstdoutを握り続け `for line in proc.stdout`
    がEOFを受け取れない。これを回避するため、親子孫を一括で停止する。

    POSIX: `os.killpg(pgid, SIGTERM)` → `timeout` 秒待機 → 残存に
    `os.killpg(pgid, SIGKILL)`。`start_new_session=True` によりpgidはproc.pidと
    一致するので、親が既にreapされていてもpid=pgidとして停止シグナルを届けられる。

    Windows: 完全なJob Objectを導入しない簡易実装。親消失後に `children(recursive=True)`
    では子孫を辿れないため、先に列挙して `terminate()` を送り、その後 `wait_procs` で
    残存に `kill()` を送る。サブプロセスが更に分離Job Objectを使う場合は取り逃すが、
    現状のpyfltr対応ツールでは問題にならない範囲とする。
    """
    targets: list[psutil.Process] = []
    if os.name == "nt":
        # 親消失後に辿れなくなるため、事前に子孫pid集合を取得する。
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            parent = psutil.Process(proc.pid)
            targets = parent.children(recursive=True)
        with contextlib.suppress(OSError):
            proc.terminate()
        for child in targets:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.terminate()
    else:
        # os.killpg / os.getpgid / signal.SIGKILLはPOSIX専用でWindows型スタブに未定義。
        # os.nameガード下なので実行時は安全。型チェッカーの誤検知だけ局所コメントで抑止する。
        try:
            pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        except ProcessLookupError:
            # 親プロセスが既にreapされている。start_new_session=Trueにより
            # pgid == pidとして設定されていたはずなのでpidをそのまま使う。
            pgid = proc.pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member

    # psutil.Processは失敗時も自身を含めて扱うためNoneチェックのうえでwait対象に含める。
    wait_targets: list[psutil.Process] = list(targets)
    with contextlib.suppress(psutil.NoSuchProcess):
        wait_targets.append(psutil.Process(proc.pid))

    _, alive = psutil.wait_procs(wait_targets, timeout=timeout)

    # 残存プロセスへSIGKILL / killを送る。
    if alive:
        if os.name == "nt":
            for child in alive:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    child.kill()
        else:
            try:
                pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
            except ProcessLookupError:
                pgid = proc.pid
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        _, still_alive = psutil.wait_procs(alive, timeout=timeout)
        if still_alive:
            remaining_pids = [p.pid for p in still_alive]
            logger.warning("プロセスツリー停止後に残存するプロセスあり: pids=%s", remaining_pids)


atexit.register(_DEFAULT_REGISTRY.cleanup)


def terminate_active_processes(*, timeout: float = 5.0) -> None:
    """実行中のすべての子プロセスと子孫にterminate() → kill() を送る。

    --fail-fastやTUI Ctrl+C協調停止で、並列実行中の他ツールを止めるために呼ばれる。
    `_kill_process_tree` 経由でプロセスグループ単位 （POSIX） / 子孫pid列挙 （Windows）
    で停止するため、pytest-xdistのようにPopen子が更にサブプロセスをforkする
    ツールでも確実に停止する。
    """
    _DEFAULT_REGISTRY.terminate_all(timeout=timeout)


@dataclasses.dataclass
class ExecutionBaseContext:
    """実行パイプライン全体で不変のコンテキスト。

    `run_pipeline` が1回だけ組み立て、CLI/TUI各経路へ渡す。
    """

    config: pyfltr.config.Config
    """実行設定（pyproject.tomlから読み込んだ設定値）。"""
    all_files: "list[pathlib.Path]"
    """対象ファイル一覧（ディレクトリ走査・excludeフィルタリング済み）。"""
    cache_store: "pyfltr.cache.CacheStore | None"
    """ファイルhashキャッシュストア。`None` の場合はキャッシュ無効。"""
    cache_run_id: str | None
    """キャッシュ書き込み時の参照元run_id。`None` の場合はキャッシュ書き込みをスキップ。"""


@dataclasses.dataclass
class ExecutionContext:
    """コマンド実行ごとに変動するコンテキスト。

    `ExecutionBaseContext` を包みつつ、各コマンド実行直前に組み立てる。
    CLI経路では `_run_one_command` が、TUI経路では `UIApp._execute_command` が組み立てる。
    """

    base: ExecutionBaseContext
    """パイプライン全体で不変のコンテキスト。"""
    fix_stage: bool = False
    """fixステージとして実行するか（fix-argsを適用して単発fix経路で動作する）。"""
    only_failed_targets: "pyfltr.only_failed.ToolTargets | None" = None
    """`--only-failed` 経路でのツール別失敗ファイル集合。`None` の場合は `all_files` を使用。"""
    on_output: "typing.Callable[[str], None] | None" = None
    """サブプロセス出力の逐次コールバック。TUI経路でリアルタイム表示に使用。"""
    is_interrupted: "typing.Callable[[], bool] | None" = None
    """中断指示の確認コールバック。TUI協調停止経路で使用。"""
    on_subprocess_start: "typing.Callable[[], None] | None" = None
    """サブプロセス起動直後のフック。TUI経路で実行中コマンド集合を追跡するのに使用。"""
    on_subprocess_end: "typing.Callable[[], None] | None" = None
    """サブプロセス終了直前のフック。`on_subprocess_start` と対になる。"""

    @property
    def config(self) -> pyfltr.config.Config:
        """`base.config` への委譲。"""
        return self.base.config

    @property
    def all_files(self) -> "list[pathlib.Path]":
        """`base.all_files` への委譲。"""
        return self.base.all_files

    @property
    def cache_store(self) -> "pyfltr.cache.CacheStore | None":
        """`base.cache_store` への委譲。"""
        return self.base.cache_store

    @property
    def cache_run_id(self) -> "str | None":
        """`base.cache_run_id` への委譲。"""
        return self.base.cache_run_id


@dataclasses.dataclass
class CommandResult:
    """コマンドの実行結果。"""

    command: str
    command_type: str
    commandline: list[str]
    returncode: int | None
    has_error: bool
    files: int
    output: str
    elapsed: float
    errors: list[pyfltr.error_parser.ErrorLocation] = dataclasses.field(default_factory=list)
    target_files: list[pathlib.Path] = dataclasses.field(default_factory=list)
    """当該ツールに渡したターゲットファイル一覧 （retry_commandの位置引数復元に使用）。

    `pass-filenames=False` のツールでは `commandline` にファイルが含まれないため、
    retry_commandでターゲットを差し替えるには実行時点のリストを別途保持する必要がある。
    """
    archived: bool = False
    """実行アーカイブへの書き込みに成功したか。

    `True` のときに限り、JSONL側でsmart truncationによるメッセージ/diagnostic省略を
    適用できる （切り詰め分はアーカイブから復元可能）。`--no-archive` やアーカイブ初期化
    失敗時は `False` のままとなり、切り詰めをスキップして全文をJSONLに出力する。
    """
    retry_command: str | None = None
    """当該ツール1件を再実行するためのshellコマンド文字列 （toolレコード用）。

    `run_pipeline` がツール完了時に埋める。未設定 （`None`） のときはtoolレコードから
    省略する （テスト等、パイプライン外でCommandResultを生成する場合）。
    """
    cached: bool = False
    """ファイルhashキャッシュから復元された結果か否か。

    `True` のとき、当該ツールは実際には実行されておらず、過去の実行結果を復元して
    返されている。`--no-cache` またはキャッシュ未ヒットの場合は `False`。
    """
    cached_from: str | None = None
    """キャッシュヒット時の復元元run_id （ULID）。

    `cached=True` のときに限り設定される。JSONL toolレコードで参照誘導用に出力する
    （`show-run` / MCPの詳細参照経路で当該runの全文を確認できる）。
    """
    fixed_files: list[str] = dataclasses.field(default_factory=list)
    """fixステージ・formatterステージで実際にファイル内容が変化した対象のパス一覧。

    内容ハッシュ比較により変化を検知して設定する。
    内容変化が検知されなかった場合は空。
    `summary.applied_fixes` の集計に使用する。
    """
    resolution_failed: bool = False
    """ツール起動コマンドの解決に失敗したか。

    bin-runner / js-runnerからの解決失敗時に `True` を立てる。
    `status` プロパティは通常の実行失敗（`failed`）より優先して
    `resolution_failed` を返し、CIログ等で「対象0件で失敗したのか／
    対象はあったが解決に失敗したのか」を区別可能にする。
    """

    @classmethod
    def from_run(  # pylint: disable=duplicate-code
        cls,
        *,
        command: str,
        command_info: "pyfltr.config.CommandInfo | None" = None,
        commandline: list[str],
        returncode: int | None,
        output: str,
        elapsed: float,
        files: int,
        has_error: bool = False,
        errors: "list[pyfltr.error_parser.ErrorLocation] | None" = None,
        command_type: str | None = None,
        resolution_failed: bool = False,
    ) -> "CommandResult":
        """実行結果からCommandResultを組み立てるファクトリメソッド。

        `command_type` を省略した場合は `command_info.type` を使う。
        `command_type` と `command_info` の両方を省略することはできない。
        `errors` を省略した場合は空リストを使う（parse_errorsの呼び出しは呼び出し側で行う）。
        """
        resolved_type: str
        if command_type is None:
            assert command_info is not None, "command_type と command_info のどちらかを指定する必要がある"
            resolved_type = command_info.type
        else:
            resolved_type = command_type
        return cls(
            command=command,
            command_type=resolved_type,
            commandline=commandline,
            returncode=returncode,
            has_error=has_error,
            files=files,
            output=output,
            elapsed=elapsed,
            errors=errors if errors is not None else [],
            resolution_failed=resolution_failed,
        )

    @property
    def alerted(self) -> bool:
        """skipped/succeeded以外ならTrue"""
        return self.returncode is not None and self.returncode != 0

    @property
    def status(self) -> str:
        """ステータスの文字列を返す。"""
        if self.resolution_failed:
            return "resolution_failed"
        if self.returncode is None:
            status = "skipped"
        elif self.returncode == 0:
            status = "succeeded"
        elif self.command_type == "formatter" and not self.has_error:
            status = "formatted"
        else:
            status = "failed"
        return status

    def get_status_text(self) -> str:
        """成型した文字列を返す。"""
        return f"{self.status} ({self.files}files in {self.elapsed:.1f}s)"


def _resolve_js_commandline(
    command: str,
    config: pyfltr.config.Config,
) -> tuple[str, list[str]]:
    """JSツール （textlint / markdownlint） の実行ファイルと引数prefixを決定する。

    `{command}-path` が空のときに呼び出され、`js-runner` 設定に基づいて
    起動コマンドを組み立てる。`direct` モードで `node_modules/.bin/<cmd>` が
    存在しない場合は `FileNotFoundError` を送出する。
    """
    bin_name = _JS_TOOL_BIN[command]
    runner = config["js-runner"]
    # 汎用化: `{command}-packages` キーを参照することで任意のJSツールで
    # `--package` / `-p` 展開を利用可能にする。未定義キーは空リスト扱い。
    packages: list[str] = list(config.values.get(f"{command}-packages", []))

    if runner == "pnpx":
        main_spec = _JS_TOOL_PNPX_PACKAGE_SPEC.get(command, bin_name)
        prefix: list[str] = ["--package", main_spec]
        for pkg in packages:
            prefix.extend(["--package", pkg])
        prefix.append(bin_name)
        return "pnpx", prefix
    if runner == "pnpm":
        return "pnpm", ["exec", bin_name]
    if runner == "npm":
        return "npm", ["exec", "--no", "--", bin_name]
    if runner == "npx":
        prefix = ["--no-install"]
        for pkg in packages:
            prefix.extend(["-p", pkg])
        prefix.extend(["--", bin_name])
        return "npx", prefix
    if runner == "yarn":
        return "yarn", ["run", bin_name]
    if runner == "direct":
        bin_dir = pathlib.Path("node_modules") / ".bin"
        # Windowsでは `.cmd` 付きのラッパーを優先する。pyrightの静的評価では
        # Linux上だと `sys.platform == "win32"` 側の分岐をunreachableとみなすため、
        # `os.name` を経由して静的分岐とみなされないようにする。
        candidates: list[pathlib.Path] = []
        if os.name == "nt":
            candidates.append(bin_dir / f"{bin_name}.cmd")
        candidates.append(bin_dir / bin_name)
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate), []
        raise FileNotFoundError(str(candidates[0]))
    raise ValueError(f"js-runnerの設定値が正しくありません: {runner=}")


def _get_env_path(env: dict[str, str]) -> str | None:
    """`env` からPATH値を取り出す。

    Windowsは環境変数名が大文字小文字非区別のため `env` キーを非依存探索する
    （`env={"Path": "..."}` のように大小が混在していても拾う）。POSIXで同じ探索を
    行うと `env={"Path": "/tmp/bin", "PATH": "/usr/bin"}` のようなケースで解決側と
    Popen実行時側のPATHが不一致となるため、POSIXでは `env.get("PATH")` のみを
    使う。
    """
    if os.name == "nt":
        for key, value in env.items():
            if key.upper() == "PATH":
                return value
        return None
    return env.get("PATH")


# miseが親プロセスのPATHに注入するtoolパスのマーカー。
# パス区切りを `/` に正規化したうえで含有判定する。`mise/bin` はmise本体の
# バイナリディレクトリのため保護対象（このリストに含めない）。
# 詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
_MISE_TOOL_PATH_MARKERS: tuple[str, ...] = (
    "/mise/installs/",
    "/mise/dotnet-root",
    "/mise/shims",
)


def _normalize_path_entry_for_dedup(entry: str) -> str:
    r"""重複排除用の比較キーを返す。

    Windowsではパス比較が大文字小文字非区別かつ `/` と `\\` を等価扱いするため、
    両者を吸収して比較する。POSIXでは大文字小文字を保ったまま末尾スラッシュのみ落とす。
    """
    if os.name == "nt":
        return entry.replace("/", "\\").rstrip("\\").lower()
    return entry.rstrip("/")


def _detect_path_key(env: "typing.Mapping[str, str]") -> str | None:
    """`env` からPATHのキー名を検出する。

    Windowsは環境変数名が大文字小文字非区別のため `Path` / `PATH` のいずれかが
    入りうる。書き戻し時に元のキー名を保つため、検出した名前をそのまま返す。
    POSIXでは `"PATH"` 固定で扱う（大小混在エントリの不整合を避けるため）。
    """
    if os.name == "nt":
        for key in env:
            if key.upper() == "PATH":
                return key
        return None
    return "PATH" if "PATH" in env else None


def _dedupe_path_value(path_value: str) -> str:
    """順序先勝ちでPATH文字列を重複排除する。

    比較キーは `_normalize_path_entry_for_dedup` でOS依存に正規化する。
    空エントリ（POSIXでcwd相当）は最初の1回だけ保持する。
    """
    sep = os.pathsep
    seen: set[str] = set()
    result: list[str] = []
    for entry in path_value.split(sep):
        key = _normalize_path_entry_for_dedup(entry)
        if key in seen:
            continue
        seen.add(key)
        result.append(entry)
    return sep.join(result)


def dedupe_environ_path(environ: "typing.MutableMapping[str, str]") -> bool:
    """`environ` のPATHを順序先勝ちで重複排除し、同一キー名で書き戻す。

    CLIエントリポイントから1度だけ呼ぶ前提のヘルパー。プロセス内で起動する
    全subprocessは `os.environ` を継承するため、ここで整えれば波及する。
    WindowsでのPATHキー名揺れ（`Path` / `PATH`）は `_detect_path_key`
    が吸収し、検出した名前のまま書き戻す。

    Returns:
        書き換えが発生したらTrue、PATH未設定または変更不要ならFalse。
    """
    key = _detect_path_key(environ)
    if key is None:
        return False
    original = environ[key]
    deduped = _dedupe_path_value(original)
    if original == deduped:
        return False
    environ[key] = deduped
    return True


def _is_mise_tool_path(entry: str) -> bool:
    """エントリがmiseの注入したtoolパスかを判定する。

    対象は `mise/installs/` 配下・`mise/dotnet-root`・`mise/shims`。
    mise本体バイナリディレクトリ（`mise/bin`）は対象外として保護する。
    Windowsでの大文字小文字差・パス区切り差を吸収する。
    """
    if entry == "":
        return False
    normalized = entry.replace("\\", "/")
    if os.name == "nt":
        normalized = normalized.lower()
    return any(marker in normalized for marker in _MISE_TOOL_PATH_MARKERS)


def _strip_mise_tool_paths(path_value: str) -> str:
    """PATH文字列からmise toolパスを除外して返す。"""
    sep = os.pathsep
    return sep.join(entry for entry in path_value.split(sep) if not _is_mise_tool_path(entry))


def _build_mise_subprocess_env(env: dict[str, str]) -> dict[str, str]:
    """`env` のコピーからmise toolパスを除外したenvを返す。

    入力 `env` は破壊しない（純関数）。PATH未設定時は単にコピーを返す。
    本処理は `mise exec` 経由のsubprocessに対してのみ適用する。
    PATH上にmiseのtoolエントリ（installs / dotnet-root / shims）が見えていると
    miseがtools解決をスキップしてPATH解決にフォールバックする挙動を起こすため、
    指定バージョンのSDKが選ばれない不安定挙動を防ぐ目的で除外する。
    詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
    """
    new_env = env.copy()
    key = _detect_path_key(new_env)
    if key is None:
        return new_env
    new_env[key] = _strip_mise_tool_paths(new_env[key])
    return new_env


def _terminate_and_drop(proc: "subprocess.Popen[str]") -> None:
    """実行中procとその子孫を停止し `_active_processes` から外す。

    TUI協調停止経路で使う。`with subprocess.Popen(...)` の__exit__は子が残っていても
    `wait()` で止まってしまうため、`InterruptedExecution` を送出する前に本関数で
    確実に子を終了させる。pytest-xdistなど孫プロセスをforkするツールを想定し、
    `_kill_process_tree` でプロセスツリー単位で停止する。
    """
    with contextlib.suppress(OSError):
        _kill_process_tree(proc, timeout=5.0)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5.0)
    _DEFAULT_REGISTRY.remove(proc)


def _run_subprocess(
    commandline: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None = None,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """サブプロセスの実行 （Popenベース）。

    --fail-fastで並列実行中の他プロセスを外部スレッドからterminate() できるよう、
    subprocess.runの経路もPopenに統一し `_active_processes` に登録する。
    `on_output` が指定されている場合は逐次コールバックを呼び、未指定時は最後に
    全出力をまとめて返す。

    `is_interrupted` が指定された場合、（1） `Popen` 呼び出し直前、（2） `Popen` 生成直後、
    （3） stdout読み出しループの各イテレーション冒頭の3点で中断指示を確認し、真なら
    当該procを確実に終了させてから `InterruptedExecution` を送出する。TUI協調停止経路で
    使う。`on_subprocess_start` / `on_subprocess_end` はsubprocessが実際に動いている
    区間を追跡するためのフック（UI側で「実行中コマンド集合」を正確に保つのに使う）。
    start後は必ずfinallyでendを呼ぶため、Ctrl+Cスナップショットにフック外の時間帯が
    混入しない。

    Windowsでは `subprocess.Popen` を `shell=False` でリスト渡しにすると
    `.exe` / `.cmd` 等の拡張子付きファイルをPATHから自動解決しないため、
    ここで `shutil.which` を使って `commandline[0]` をフルパスへ解決する。
    引数の `commandline` は書き換えず、Popenに渡す一時リストのみで差し替える
    （CommandResult.commandlineやretry_commandに解決後のフルパスが混入して
    ポータビリティが損なわれるのを避けるため）。解決探索対象PATHはPopenに
    渡す `env` のPATH値と一致させる（隔離したenvで見えない実行ファイルを
    起動したり、逆にenvでだけ見える実行ファイルを解決できない事故を避ける）。
    Windowsでは環境変数名が大文字小文字非区別のためenvキーを非依存探索する。
    解決できなかった場合は元のコマンド名のままPopenに渡し、既存の
    FileNotFoundError経路でrc=127の `CompletedProcess` に変換する。
    """
    popen_commandline = commandline
    env_path = _get_env_path(env)
    resolved = shutil.which(commandline[0], path=env_path)
    if resolved is not None and resolved != commandline[0]:
        popen_commandline = [resolved, *commandline[1:]]
    # （1） Popen直前の中断チェック。procがまだ存在しないのでそのまま送出できる。
    if is_interrupted is not None and is_interrupted():
        raise InterruptedExecution
    # OS別のプロセスグループ分離オプション。pytest-xdistなど孫プロセスを
    # forkするツールの中断時に、親子孫をまとめて停止できるようにする。
    popen_extra: dict[str, typing.Any] = {}
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUPはWindows専用の定数。getattrの3引数形式を使うと
        # ruff B009のgetattr→属性アクセス変換対象外になるため、型チェッカー誤検知を回避できる。
        popen_extra["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_extra["start_new_session"] = True
    try:
        with subprocess.Popen(
            popen_commandline,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            **popen_extra,
        ) as proc:
            _DEFAULT_REGISTRY.add(proc)
            subprocess_started = False
            try:
                if on_subprocess_start is not None:
                    on_subprocess_start()
                subprocess_started = True
                # （2） Popen生成直後の中断チェック。_active_processes登録済みなので
                # _terminate_and_dropで自己登録を外してから送出する。
                if is_interrupted is not None and is_interrupted():
                    _terminate_and_drop(proc)
                    raise InterruptedExecution

                output_lines: list[str] = []
                assert proc.stdout is not None
                for line in proc.stdout:
                    # （3） 各イテレーション冒頭の中断チェック。
                    if is_interrupted is not None and is_interrupted():
                        _terminate_and_drop(proc)
                        raise InterruptedExecution
                    output_lines.append(line)
                    if on_output is not None:
                        on_output(line)
                proc.wait()
                return subprocess.CompletedProcess(
                    args=commandline,
                    returncode=proc.returncode,
                    stdout="".join(output_lines),
                )
            finally:
                if subprocess_started and on_subprocess_end is not None:
                    on_subprocess_end()
                _DEFAULT_REGISTRY.remove(proc)
    except FileNotFoundError as e:
        message = f"実行ファイルが見つかりません: {commandline[0]} ({e})\n"
        if on_output is not None:
            on_output(message)
        return subprocess.CompletedProcess(
            args=commandline,
            returncode=127,
            stdout=message,
        )


def pick_targets(
    only_failed_targets: "dict[str, pyfltr.only_failed.ToolTargets] | None",
    command: str,
) -> "pyfltr.only_failed.ToolTargets | None":
    """`only_failed_targets` から当該ツールのToolTargetsを取り出す。

    `only_failed_targets` 自体が `None` の場合（`--only-failed` 未指定）は常に
    `None` を返し、`execute_command` で既定の `all_files` に委ねる。指定あり時は
    dictから当該コマンドのエントリを返す（存在しない場合はNone）。
    `cli` と `ui` の両経路から同一挙動で引ける共通ヘルパー。
    """
    if only_failed_targets is None:
        return None
    return only_failed_targets.get(command)


@dataclasses.dataclass(frozen=True)
class _ExecutionParams:
    """`execute_command` の共通前処理結果。

    ターゲット解決・コマンドライン構築を済ませた中間状態を保持し、
    dispatcherと各runner関数で参照する。
    """

    command_info: pyfltr.config.CommandInfo
    targets: list[pathlib.Path]
    commandline_prefix: list[str]
    commandline: list[str]
    additional_args: list[str]
    fix_mode: bool
    fix_args: list[str] | None
    via_mise: bool = False
    """このコマンドが `mise exec` 経由で起動されるか。

    `ensure_mise_available` 通過後の `ResolvedCommandline` から判定する
    （mise不在時にdirectへフォールバックされたケースを除外するため、
    `build_commandline` 直後の値ではなく事後の値で判断する）。
    `_build_subprocess_env` でのmise toolパス除外適用判断に使う。
    詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
    """


def build_invocation_argv(
    command: str,
    config: pyfltr.config.Config,
    commandline_prefix: list[str],
    additional_args: list[str],
    *,
    fix_stage: bool,
) -> list[str]:
    """対象ファイル抜きの起動argvを構築する共通ヘルパー。

    通常段では `[prefix] + auto_args + {command}-args + {command}-lint-args + additional_args`
    に構造化出力引数を適用した結果を返す。fix段では `{command}-fix-args` を結合する。
    fix-args未定義のコマンドではfix_stage=Trueでも通常段と同じargvを返す。

    textlintのfix段は `_execute_textlint_fix` のStep1と同じ規則
    （`--format` ペアを除去したargs + fix-args、auto_args / 構造化出力引数なし）を適用する。
    実行本体（`_prepare_execution_params` / `_execute_textlint_fix`）と
    `command-info` 表示の双方から本ヘルパーへ集約することで、組み立て規則の重複定義を避ける。
    """
    user_args: list[str] = list(config.values.get(f"{command}-args", []))
    extra: list[str] = list(additional_args)

    # textlintのfix Step1はfixer-formatterがcompact系をサポートしないため、
    # ユーザー指定の `--format` ペアを一律で除去したうえでfix-argsを結合する特殊経路。
    # auto_args・構造化出力引数も適用しない（fixer出力の解析は本ステップでは行わないため）。
    if fix_stage and command == "textlint":
        fix_args: list[str] = list(config.values.get(f"{command}-fix-args", []))
        return [
            *commandline_prefix,
            *_strip_format_option(user_args),
            *fix_args,
            *extra,
        ]

    fix_args_value: list[str] | None = None
    if fix_stage:
        raw = config.values.get(f"{command}-fix-args")
        fix_args_value = list(raw) if raw is not None else None

    auto_args = _build_auto_args(command, config, user_args + extra)
    commandline: list[str] = [*commandline_prefix, *auto_args, *user_args]
    if fix_args_value is not None:
        commandline.extend(fix_args_value)
    else:
        commandline.extend(config.values.get(f"{command}-lint-args", []))
    commandline.extend(extra)
    structured_spec = _get_structured_output_spec(command, config)
    if structured_spec is not None and not (structured_spec.lint_only and fix_args_value is not None):
        commandline = _apply_structured_output(commandline, structured_spec)
    return commandline


def _prepare_execution_params(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    all_files: list[pathlib.Path],
    *,
    fix_stage: bool,
    only_failed_targets: "pyfltr.only_failed.ToolTargets | None",
) -> "_ExecutionParams | CommandResult":
    """実行前の共通前処理を行い `_ExecutionParams` を返す。

    ツールパス解決に失敗した場合は `CommandResult` を直接返す。
    ターゲット0件の場合は `_ExecutionParams` を返し（targetsが空リスト）、
    呼び出し側でスキップ処理を行う。
    """
    command_info = config.commands[command]
    globs = command_info.target_globs()
    source_files = only_failed_targets.resolve_files(all_files) if only_failed_targets is not None else all_files
    targets: list[pathlib.Path] = filter_by_globs(source_files, globs)

    # ツール別excludeの適用（--no-excludeが指定された場合はスキップ）
    if not args.no_exclude:
        tool_excludes: list[str] = config.values.get(f"{command}-exclude", [])
        if tool_excludes:
            targets = [t for t in targets if not _matches_exclude_patterns(t, tool_excludes)]

    # ファイルの順番をシャッフルまたはソート（fixステージは再現性重視でシャッフルを無効化）
    if args.shuffle and not fix_stage:
        random.shuffle(targets)
    else:
        # natsort.natsortedの型ヒントが不十分でtyがunion型へ縮めるためcastで明示。
        targets = typing.cast("list[pathlib.Path]", natsort.natsorted(targets, key=str))

    # fixステージでは当該コマンドのfix-argsを引用してfix経路に分岐する。
    # fix-args未定義のformatterは通常経路を通る（通常実行でもファイルを書き換えるため挙動は同じ）。
    fix_mode = fix_stage
    fix_args: list[str] | None = None
    if fix_mode:
        fix_args = config.values.get(f"{command}-fix-args")

    # 対象ファイル0件ならこの後の実行自体が走らないため、ツールパス解決を省略する。
    # mise等のbin-runner解決はネットワークやプラットフォーム制約で失敗し得るため、
    # 解決不要な状況で副作用的な失敗を出さないよう早期返却する。
    if not targets:
        return _ExecutionParams(
            command_info=command_info,
            targets=targets,
            commandline_prefix=[],
            commandline=[],
            additional_args=[],
            fix_mode=fix_mode,
            fix_args=fix_args,
            via_mise=False,
        )

    # `{command}-runner` および `{command}-path` 設定からツール起動コマンドラインを解決する。
    # bin-runner経路（mise / direct / グローバル `bin-runner` 委譲）とjs-runner経路、
    # 直接実行を統一的に扱う。mise経路では事前可用性チェック（mise exec --version）も実行する。
    try:
        # 実コマンド実行経路はmise副作用を許可し、mise設定判定の `mise ls --current --json` でも
        # `mise-auto-trust` に従ったtrust→再実行を可能にする。
        resolved = build_commandline(command, config, allow_side_effects=True)
        resolved = ensure_mise_available(resolved, config, command=command)
    except ValueError as e:
        return _failed_resolution_result(command, command_info, str(e), files=len(targets))
    except FileNotFoundError as e:
        if command in _JS_TOOL_BIN and config["js-runner"] == "direct":
            message = (
                f"js-runner=direct 指定ですが実行ファイルが見つかりません: {e}. "
                "package.jsonで対象パッケージをインストールしてください。"
            )
        else:
            message = f"ツールが見つかりません: {e}"
        return _failed_resolution_result(command, command_info, message, files=len(targets))
    commandline_prefix = resolved.commandline

    # 起動オプションからの追加引数 （--textlint-argsなど） をshlex分割しておく
    additional_args_str = getattr(args, f"{command.replace('-', '_')}_args", "")
    additional_args = shlex.split(additional_args_str) if additional_args_str else []

    # 対象ファイル抜きのargvを共通ヘルパーで組み立てる:
    #   [prefix] + [auto-args] + args + (lint-args or fix-args) + additional_args + structured_output適用
    # textlintのfix経路では `_execute_textlint_fix` 側が改めてargvを組み立てるため
    # ここでの値は実際には使われない（execute_commandのdispatchでtextlint fixは別経路へ分岐する）。
    commandline = build_invocation_argv(
        command,
        config,
        commandline_prefix,
        additional_args,
        fix_stage=fix_args is not None,
    )
    # pass-filenames = falseのツールはファイル引数を渡さない（tsc等）
    if config.values.get(f"{command}-pass-filenames", True):
        commandline.extend(str(t) for t in targets)

    # `ensure_mise_available` を通過した後の `effective_runner` でmise経路かを判定する。
    # `build_commandline` 直後はmise不在時のdirectフォールバック前の値が入っているため、
    # ここでは事後値を採用する（direct経路へtoolパス除外を誤適用しないため）。
    via_mise = resolved.effective_runner == "mise" or resolved.executable == "mise"

    return _ExecutionParams(
        command_info=command_info,
        targets=targets,
        commandline_prefix=commandline_prefix,
        commandline=commandline,
        additional_args=additional_args,
        fix_mode=fix_mode,
        fix_args=fix_args,
        via_mise=via_mise,
    )


def execute_command(
    command: str,
    args: argparse.Namespace,
    ctx: ExecutionContext,
) -> CommandResult:
    """コマンドの実行。

    `fix_stage=True` の場合、当該コマンドがfix-argsを持っていればfix経路
    （`--fix` 付きの単発実行）で動作する。fix-args未定義のformatterでは
    通常経路と挙動が変わらないため、呼び出し側はfixステージで走らせる対象を
    `split_commands_for_execution()` で絞り込んだうえで指定する前提。

    `cache_store` が指定され、かつ当該コマンドが `CommandInfo.cacheable=True` の
    非fixモード実行なら、ファイルhashキャッシュを参照して一致があれば実行を
    スキップし、過去の結果を復元して `cached=True` で返す。キャッシュミス時は
    通常実行のうえ、成功 （rc=0, has_error=False） に限り `cache_run_id` をソースとして
    書き込む。`cache_run_id` が `None` の場合はキャッシュ書き込みをスキップする
    （アーカイブ無効時に `cached_from` で参照させる元runが無いため）。

    `only_failed_targets` が指定された場合、`ToolTargets.resolve_files(all_files)`
    経由で実対象ファイルを取得する（`--only-failed` 経路でツール別の失敗ファイル集合を
    渡す用途）。その後の `target_extensions` / `pass_filenames=False` の分岐は
    通常通り適用される。`None` の場合は既定の `all_files` を使用する。
    """
    # ctxから各フィールドを展開する。
    config = ctx.config
    all_files = ctx.all_files
    on_output = ctx.on_output
    is_interrupted = ctx.is_interrupted
    on_subprocess_start = ctx.on_subprocess_start
    on_subprocess_end = ctx.on_subprocess_end

    # 共通前処理: ターゲット解決・コマンドライン構築
    params_or_error = _prepare_execution_params(
        command,
        args,
        config,
        all_files,
        fix_stage=ctx.fix_stage,
        only_failed_targets=ctx.only_failed_targets,
    )
    if isinstance(params_or_error, CommandResult):
        # ツールパス解決失敗
        return params_or_error
    params = params_or_error
    command_info = params.command_info
    targets = params.targets
    commandline = params.commandline
    commandline_prefix = params.commandline_prefix
    additional_args = params.additional_args
    fix_mode = params.fix_mode
    fix_args = params.fix_args

    # 各CommandResultに当該ツールのターゲットファイル一覧を埋めるためのヘルパー。
    # retry_commandで差し替え可能なターゲットを復元するのに使う（特にpass-filenames=False
    # のツールではcommandlineからも復元できないため、ここで明示的に保持する）。
    def _with_targets(result: CommandResult) -> CommandResult:
        result.target_files = list(targets)
        return result

    if len(targets) <= 0:
        return _with_targets(
            CommandResult.from_run(
                command=command,
                command_info=command_info,
                commandline=commandline,
                returncode=None,
                output="対象ファイルが見つかりません。",
                files=0,
                elapsed=0,
            )
        )

    start_time = time.perf_counter()
    env = _build_subprocess_env(config, command, via_mise=params.via_mise)

    # pre-commitは .pre-commit-config.yamlを参照してSKIP環境変数を構築し、
    # pyfltr関連hookを除外したうえで2段階実行する。
    # stage 1でファイル修正のみ （fixer系） なら "formatted"、
    # checker系hookが残存エラーを報告すれば "failed" となる。
    if command == "pre-commit":
        return _with_targets(
            _execute_pre_commit(
                command,
                command_info,
                commandline,
                targets,
                config,
                args,
                env,
                on_output,
                start_time,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # glab-ci-lintはGitLab API経由のlintで、GitLab remote未登録の環境では
    # glab自身が非ゼロ終了しメッセージを返す。pyfltr利用者にとっては環境的事情のため、
    # failedではなくskipped相当へ書き換える。判定はglabの英語ロケール出力に
    # 依存するためLC_ALL/LANG=Cを強制する。
    if command == "glab-ci-lint":
        return _with_targets(
            _execute_glab_ci_lint(
                command,
                command_info,
                commandline,
                targets,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # textlintのfixモードは2段階実行 （fix適用 + lintチェック）。
    # fixer-formatterがcompactをサポートしない問題と、残存違反をcompactで取得する
    # 要件を両立させるため、他のlinterとは別経路で実行する。
    if fix_args is not None and command == "textlint":
        return _with_targets(
            _execute_textlint_fix(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # fixモードでlinterにfix-argsを適用する経路。
    # mtime変化でformatted判定を行い、rc != 0はそのままfailed扱いとする。
    if fix_args is not None and command_info.type != "formatter":
        return _with_targets(
            _execute_linter_fix(
                command,
                command_info,
                commandline,
                targets,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # ruff-formatでruff-format-by-checkが有効な場合は、
    # 先にruff check --fix --unsafe-fixesを実行してからruff formatを実行する。
    # ステップ1（check）のlint violation （exit 1） は無視する （lintはruff-checkで検出）。
    # ただしexit >= 2 （設定エラー等） は失敗扱いする。
    if command == "ruff-format" and config["ruff-format-by-check"]:
        return _with_targets(
            _execute_ruff_format_two_step(
                command,
                command_info,
                commandline,
                targets,
                config,
                args,
                env,
                on_output,
                start_time,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # taploはcheckとformatが排他のためshfmt同様の2段階実行。
    if command == "taplo":
        return _with_targets(
            _execute_taplo_two_step(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                fix_mode=fix_mode,
                env=env,
                on_output=on_output,
                start_time=start_time,
                args=args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # shfmtは-l （確認） と-w （書き込み） が排他のためprettier同様の2段階実行。
    if command == "shfmt":
        return _with_targets(
            _execute_shfmt_two_step(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                fix_mode=fix_mode,
                env=env,
                on_output=on_output,
                start_time=start_time,
                args=args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # prettierは--check （read-only） と--write （書き込み） が排他のため2段階実行する。
    # ruff-formatと同じ位置・スタイルで分岐する。
    # prettierには {cmd}-fix-argsを定義していないためfix判定はfix_stage由来の
    # fix_mode変数を使う （filter_fix_commandsではformatterとして常にfix対象となる）。
    if command == "prettier":
        return _with_targets(
            _execute_prettier_two_step(
                command,
                command_info,
                commandline_prefix,
                config,
                targets,
                additional_args,
                fix_mode=fix_mode,
                env=env,
                on_output=on_output,
                start_time=start_time,
                args=args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # plain経路（通常のlinter・formatter）
    return _with_targets(
        _run_plain_command(
            command,
            command_info,
            commandline,
            targets,
            additional_args,
            env,
            on_output,
            start_time,
            args,
            config,
            fix_args=fix_args,
            cache_store=ctx.cache_store,
            cache_run_id=ctx.cache_run_id,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
    )


@dataclasses.dataclass
class _CacheContext:
    """キャッシュ参照用のコンテキスト。

    `execute_command` のplain経路でのみ使う内部ヘルパー。
    """

    cache_store: "pyfltr.cache.CacheStore"
    command: str
    key: str

    def lookup(self) -> CommandResult | None:
        """キャッシュを参照する。ヒットならCommandResult、ミスならNone。"""
        return self.cache_store.get(self.command, self.key)

    def store(self, result: CommandResult, *, run_id: str | None) -> None:
        """キャッシュへ書き込む （ソースrun_id付き）。"""
        self.cache_store.put(self.command, self.key, result, run_id=run_id)


def _prepare_cache_context(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    config: pyfltr.config.Config,
    commandline: list[str],
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_args: list[str] | None,
    cache_store: "pyfltr.cache.CacheStore | None",
) -> _CacheContext | None:
    """キャッシュ参照用のキー算出。対象外の場合はNoneを返す。"""
    if cache_store is None or not command_info.cacheable or fix_args is not None:
        return None
    import pyfltr.cache  # pylint: disable=import-outside-toplevel

    if not pyfltr.cache.is_cacheable(command, config, additional_args):
        return None
    structured_spec = _get_structured_output_spec(command, config)
    key = cache_store.compute_key(
        command=command,
        commandline=commandline,
        fix_stage=False,
        structured_output=structured_spec is not None,
        target_files=targets,
        config_files=pyfltr.cache.resolve_config_files(command, config),
    )
    return _CacheContext(cache_store=cache_store, command=command, key=key)


def _run_plain_command(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    additional_args: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    *,
    fix_args: list[str] | None,
    cache_store: "pyfltr.cache.CacheStore | None",
    cache_run_id: str | None,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """通常のlinter/formatterを単発実行するplain経路。

    ファイルhashキャッシュの参照・書き込みを担う。cacheable=Trueの非fix実行のみ
    キャッシュを扱い、textlint fixなど特殊経路はこの関数を通らない。
    """
    has_error = False

    # ファイルhashキャッシュの参照 （cacheable=Trueの非fix実行のみ）。
    # キャッシュ対象判定 / キー算出 / 書き込みをbreak/resumeできるよう、結果を
    # 後段で差し替える設計とする。
    cache_context = _prepare_cache_context(
        command,
        command_info,
        config,
        commandline,
        targets,
        additional_args,
        fix_args=fix_args,
        cache_store=cache_store,
    )
    if cache_context is not None:
        cached_result = cache_context.lookup()
        if cached_result is not None:
            cached_result.target_files = list(targets)
            # 復元値のfiles / elapsedは過去実行時のもの。復元時の実ファイル数は
            # 現在のターゲットリストに合わせ直す （再実行時の対象件数表示のため）。
            cached_result.files = len(targets)
            return cached_result

    # verbose時はコマンドラインをon_output経由で出力
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
    proc = _run_subprocess(
        commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    returncode = proc.returncode

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    # エラー箇所のパース
    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)

    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )

    # キャッシュ書き込み （成功rc=0のみ）。失敗結果を記録すると再試行で同じ失敗が
    # 復元されて修正確認できなくなるため、成功時に限定する。
    if cache_context is not None and returncode == 0 and not has_error:
        cache_context.store(result, run_id=cache_run_id)

    return result


def _build_auto_args(command: str, config: pyfltr.config.Config, user_args: list[str]) -> list[str]:
    """自動引数を構築する。

    AUTO_ARGSで定義されたフラグがTrueの場合、対応する引数を返す。
    ユーザーが *-argsやCLI引数で既に同じ文字列を指定している場合はスキップする。
    """
    auto_entries = pyfltr.config.AUTO_ARGS.get(command, [])
    if not auto_entries:
        return []
    user_args_joined = " ".join(user_args)
    result: list[str] = []
    for flag_key, args in auto_entries:
        if not config.values.get(flag_key, False):
            continue
        for arg in args:
            if arg not in user_args_joined:
                result.append(arg)
    return result


def _build_subprocess_env(
    config: pyfltr.config.Config,
    command: str,
    *,
    via_mise: bool = False,
) -> dict[str, str]:
    """サブプロセス実行用の環境変数を構築。

    `via_mise=True` の場合、PATHからmiseが注入したtoolパス（installs / dotnet-root /
    shims）を除外する。これは `mise exec` 経由のサブプロセスでmiseがtools解決を
    スキップしてPATH解決にフォールバックしてしまう挙動を防ぐための対症療法。
    詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
    """
    env = os.environ.copy()
    if via_mise:
        env = _build_mise_subprocess_env(env)
    # サプライチェーン攻撃対策: パッケージ取得系ツールの最小待機期間を既定で設定する。
    # ユーザーが既に設定している場合はその値を尊重する。
    # pnpmはnpm互換のconfig環境変数方式 （NPM_CONFIG_<SNAKE_CASE>） を採る。
    env.setdefault("UV_EXCLUDE_NEWER", "1 day")
    env.setdefault("NPM_CONFIG_MINIMUM_RELEASE_AGE", "1440")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Windowsのcp932/cp1252などに依存せず、ツール側のopen()/Path.read_text() をUTF-8で動かす。
    # 例: uv-sortがpyproject.tomlをエンコーディング未指定で読み込む箇所で発生する
    # UnicodeDecodeErrorを回避する。
    env["PYTHONUTF8"] = "1"
    if config.values.get(f"{command}-devmode", False):
        env["PYTHONDEVMODE"] = "1"
    # 表示幅を適切な範囲に制限する
    # （pytestなどは一部の表示が右寄せになるのであまり大きいと見づらい）
    env["COLUMNS"] = str(min(max(shutil.get_terminal_size().columns - 4, 80), 128))
    return env


# GitLab remote未登録/未認証の状況でglab自身が出すエラー文言。
# 検出後にglab-ci-lintをskipped扱いへ書き換える根拠とする。
# 大文字小文字差を吸収するため、判定は `output.lower()` に対して行う。
_GLAB_HOST_NOT_FOUND_PATTERNS: tuple[str, ...] = (
    "none of the git remotes configured for this repository point to a known gitlab host",
    "not authenticated",
)


def _looks_like_glab_host_missing(output: str) -> bool:
    """GlabがGitLabホストを検出できなかった旨のエラーかを判定する。"""
    lowered = output.lower()
    return any(pattern in lowered for pattern in _GLAB_HOST_NOT_FOUND_PATTERNS)


def _execute_glab_ci_lint(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Glab ci lintをホスト未検出時にスキップ扱いへ変換しつつ実行する。"""
    glab_env = dict(env)
    # 文言判定がロケール依存にならないよう英語ロケールを強制する。
    glab_env["LC_ALL"] = "C"
    glab_env["LANG"] = "C"

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")

    proc = _run_subprocess(
        commandline,
        glab_env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    if returncode != 0 and _looks_like_glab_host_missing(output):
        message = "glab がGitLabホストを検出できなかったためスキップしました。"
        pyfltr.warnings_.emit_warning(source=command, message=message)
        skip_output = f"{message}\n\n{output}" if output else message
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=None,
            output=skip_output,
            files=len(targets),
            elapsed=elapsed,
        )

    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=commandline,
        returncode=returncode,
        output=output,
        elapsed=elapsed,
        files=len(targets),
        errors=errors,
    )


def _execute_pre_commit(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.Config,
    args: argparse.Namespace,
    env: dict[str, str] | None,
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """pre-commitの2段階実行。

    stage 1でpre-commit run --all-filesを実行し、fixer系hookがファイルを
    修正しただけなら再実行で成功する（"formatted"）。checker系hookのエラーが
    残る場合は "failed"（has_error=True）として返す。
    """
    # pre-commit配下から起動された場合は自身を再帰実行しない。
    # git commit → pre-commit → pyfltr fast → pre-commitの二重実行を防ぐ。
    if pyfltr.precommit.is_running_under_precommit():
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=None,
            output="pre-commit 配下で実行されたため pre-commit 統合をスキップしました。",
            files=len(targets),
            elapsed=time.perf_counter() - start_time,
        )

    # .pre-commit-config.yamlが存在しなければスキップ
    config_dir = pathlib.Path.cwd()
    config_path = config_dir / ".pre-commit-config.yaml"
    if not config_path.exists():
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=None,
            output=".pre-commit-config.yaml が見つかりません。",
            files=len(targets),
            elapsed=time.perf_counter() - start_time,
        )

    # SKIP環境変数を構築（pyfltr関連hookを除外して再帰を防止）
    skip_value = pyfltr.precommit.build_skip_value(config, config_dir)
    pre_commit_env = dict(env) if env is not None else dict(os.environ)
    if skip_value:
        existing_skip = pre_commit_env.get("SKIP", "")
        if existing_skip:
            pre_commit_env["SKIP"] = f"{existing_skip},{skip_value}"
        else:
            pre_commit_env["SKIP"] = skip_value

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
        if skip_value:
            on_output(f"SKIP={pre_commit_env.get('SKIP', '')}\n")

    # stage 1: 実行
    proc = _run_subprocess(
        commandline,
        pre_commit_env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    returncode = proc.returncode
    has_error = False

    # stage 2: 失敗時は再実行（fixerが修正しただけなら2回目で成功する）
    if returncode != 0:
        if args.verbose and on_output is not None:
            on_output("pre-commit: stage 2 再実行\n")
        proc = _run_subprocess(
            commandline,
            pre_commit_env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
        if proc.returncode != 0:
            returncode = proc.returncode
            has_error = True

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
    )


def _execute_linter_fix(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Fixモードでのlinter実行 （fix-argsを適用して単発実行）。

    ステータス判定:
    - returncode != 0 → failed （ファイル変化に関係なく、エラーを握りつぶさない）
    - returncode == 0かつ内容ハッシュに変化あり → formatted（command_typeを
      "formatter"に差し替えて既存のstatusプロパティに委ねる）
    - returncode == 0かつ変化なし → succeeded

    ruff-checkは残存違反があるとrc=1を返すが、この設計ではfailedとして扱う。
    未修正の違反はユーザーが後段で認識すべき情報であり、成功に寄せない方針。
    """
    del command_info  # noqa  # 呼び出し側との引数形式揃え用 （使用しない）

    digests_before = _snapshot_file_digests(targets)

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
    proc = _run_subprocess(
        commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    digests_after = _snapshot_file_digests(targets)
    changed = digests_after != digests_before

    has_error = returncode != 0
    if not has_error and changed:
        # fixが適用されたのでformatter扱いでformattedにする
        result_command_type: str = "formatter"
        returncode = 1
    else:
        result_command_type = "linter"

    errors = pyfltr.error_parser.parse_errors(command, output, None)

    result = CommandResult.from_run(
        command=command,
        command_type=result_command_type,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )
    if not has_error and changed:
        result.fixed_files = _changed_files(digests_before, digests_after)
    return result


def _execute_textlint_fix(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Textlint fixモードの2段階実行 （fix適用 → lintチェック）。

    textlintはlint実行とfix実行でフォーマッタ解決に使うパッケージが異なり
    （`@textlint/linter-formatter` と `@textlint/fixer-formatter`）、fixer側は
    `compact` フォーマッタをサポートしない。このため `textlint --format compact --fix`
    がクラッシュする。また `textlint --fix` の既定出力 （stylish） は本ツールの
    builtinパーサ （compact前提） で解析できないため、残存違反を取得するには
    別途lint実行を行う必要がある。

    上記を両立させるため本関数では次の2段階を直列実行する。

    Step1: fix適用
        commandline_prefix + （textlint-argsから--formatペアを除去） + fix-args
        + additional_args + targets

    Step2: lintチェック （残存違反をcompact形式で取得）
        commandline_prefix + textlint-args + textlint-lint-args + additional_args + targets

    ステータス判定:
    -いずれかのステップがrc>=2 （致命的エラー） → failed
    - Step2 rc != 0 （残存違反あり） → failed （Errorsタブに反映される）
    - Step2 rc == 0かつStep1で内容ハッシュに変化あり → formatted
    - Step2 rc == 0かつ変化なし → succeeded

    textlint --fixは残存違反がなくても対象ファイルを書き戻すことがあり、
    mtimeベースの比較では偽陽性になる。このため内容ハッシュ
    （`_snapshot_file_digests`） で比較している。
    """
    target_strs = [str(t) for t in targets]

    # Step1: --format Xペアを除去した共通args + fix-argsでfix適用
    # `build_invocation_argv` のtextlint fix特殊経路と同じ規則を適用する。
    step1_commandline: list[str] = [
        *build_invocation_argv(command, config, commandline_prefix, additional_args, fix_stage=True),
        *target_strs,
    ]

    digests_before = _snapshot_file_digests(targets)
    # 保護対象識別子の事前検出 （Step1で破損するケースを捕捉するため）。
    # 空リスト設定時は計測を省略する。
    protected_identifiers: list[str] = list(config.values.get("textlint-protected-identifiers", []))
    contents_before: dict[pathlib.Path, str] = _snapshot_file_texts(targets) if protected_identifiers else {}

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step1_commandline)}\n")
    step1_proc = _run_subprocess(
        step1_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode
    # rc=0 （違反なし） / rc=1 （違反残存） は通常終了、rc>=2は致命的エラー扱い
    step1_fatal = step1_rc >= 2
    digests_after_step1 = _snapshot_file_digests(targets)
    step1_changed = digests_after_step1 != digests_before

    if protected_identifiers and step1_changed:
        _warn_protected_identifier_corruption(contents_before, _snapshot_file_texts(targets), protected_identifiers)

    # Step2: 通常lint実行 （残存違反を取得）
    # `build_invocation_argv` の通常段経路と同じ規則を適用する
    # （auto_argsはtextlintには未登録のため空。構造化出力引数もlint段なので通常通り適用される）。
    step2_commandline: list[str] = [
        *build_invocation_argv(command, config, commandline_prefix, additional_args, fix_stage=False),
        *target_strs,
    ]

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step2_commandline)}\n")
    step2_proc = _run_subprocess(
        step2_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step2_rc = step2_proc.returncode
    step2_fatal = step2_rc >= 2

    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    # Step2出力 （compact形式） から残存違反をパースする
    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)

    # ステータス判定
    if step1_fatal or step2_fatal:
        has_error = True
        returncode: int = step1_rc if step1_fatal else step2_rc
        result_command_type: str = "linter"
    elif step2_rc != 0:
        has_error = True
        returncode = step2_rc
        result_command_type = "linter"
    elif step1_changed:
        # fix適用済み、残存違反なし → formatted扱いにする
        has_error = False
        returncode = 1
        result_command_type = "formatter"
    else:
        has_error = False
        returncode = 0
        result_command_type = "linter"

    result = CommandResult.from_run(
        command=command,
        command_type=result_command_type,
        commandline=step2_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )
    if not has_error and step1_changed:
        result.fixed_files = _changed_files(digests_before, digests_after_step1)
    return result


def _strip_format_option(args: list[str]) -> list[str]:
    """引数列から `--format X` / `-f X` / `--format=X` を除去する （順序は保持）。

    textlintのfix実行時に使用する。`@textlint/fixer-formatter` はリンター側と
    異なるフォーマッタセットを持つため、ユーザーが共通argsに `--format compact` 等を
    指定していてもクラッシュしないように一律で除去する。compact文字列を特別扱いしないのは、
    `--format json` などの組み合わせに対しても安全に振る舞うため。
    """
    result: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--format", "-f"):
            skip_next = True
            continue
        if arg.startswith("--format="):
            continue
        result.append(arg)
    return result


def _execute_ruff_format_two_step(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    format_commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.Config,
    args: argparse.Namespace,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """ruff-formatの2段階実行 （ruff check --fix → ruff format）。

    ステップ1 （ruff check --fix --unsafe-fixes） の未修正lint violationは無視する。
    別途ruff-checkコマンドで検出される前提。ただしexit >= 2 （設定ミス等） はfailed扱い。
    ステップ1の成否にかかわらずステップ2 （ruff format） は実行する
    （対象ファイル全体のformat適用を止めないため）。
    """
    # ステップ1のコマンドライン組立
    check_commandline: list[str] = [config["ruff-format-path"]]
    check_commandline.extend(config["ruff-format-check-args"])
    check_commandline.extend(str(t) for t in targets)

    # ステップ1実行前の内容ハッシュを記録 （修正適用検知用）
    digests_before = _snapshot_file_digests(targets)

    # ステップ1実行
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = _run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode
    step1_failed = step1_rc >= 2  # exit 0/1は無視、2以上 （abrupt termination） のみ失敗扱い
    digests_after_step1 = _snapshot_file_digests(targets)
    step1_changed = digests_after_step1 != digests_before

    # ステップ2実行 （常に実行）
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(format_commandline)}\n")
    step2_proc = _run_subprocess(
        format_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step2_rc = step2_proc.returncode
    step2_formatted = step2_rc == 1
    step2_failed = step2_rc >= 2

    # 出力の合成
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    # 最終判定
    has_error = step1_failed or step2_failed
    if has_error:
        returncode = step1_rc if step1_failed else step2_rc
    elif step1_changed or step2_formatted:
        returncode = 1
    else:
        returncode = 0

    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)

    # commandlineは代表として「最後に実行したステップ」（= ruff format） を格納。
    # 両ステップ分のcommandlineはverbose出力で確認可能。
    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=format_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )
    if not has_error and (step1_changed or step2_formatted):
        # digests_beforeはStep1前のスナップショット（関数冒頭で取得済み）。
        # Step1（ruff --checkによる暗黙fix）とStep2（ruff format）の累積差分を一括で取る。
        digests_after_step2 = _snapshot_file_digests(targets)
        result.fixed_files = _changed_files(digests_before, digests_after_step2)
    return result


def _execute_taplo_two_step(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_mode: bool,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Taploの2段階実行 （taplo check → taplo format）。

    shfmtと同様、確認用サブコマンド （check） と書き込み用サブコマンド （format） が
    排他のため専用経路で処理する。

    通常モード （fix_mode=False）:

    - Step1: `prefix + args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded （整形不要）
    - Step1 rc != 0 → Step2 `prefix + args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted （整形成功）
      - Step2 rc != 0 → failed

    fixモード （fix_mode=True）:

    - Step1をスキップし、直接write-args付きで実行
    -内容ハッシュスナップショットで書き込みを検知
    """
    common_args: list[str] = list(config[f"{command}-args"])
    check_args: list[str] = list(config[f"{command}-check-args"])
    write_args: list[str] = list(config[f"{command}-write-args"])
    target_strs = [str(t) for t in targets]

    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]

    if fix_mode:
        digests_before = _snapshot_file_digests(targets)
        if args.verbose and on_output is not None:
            on_output(f"commandline: {shlex.join(write_commandline)}\n")
        write_proc = _run_subprocess(
            write_commandline,
            env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
        write_rc = write_proc.returncode
        output = write_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        digests_after = _snapshot_file_digests(targets)
        changed = digests_after != digests_before

        if write_rc != 0:
            has_error = True
            returncode: int = write_rc
        elif changed:
            has_error = False
            returncode = 1
        else:
            has_error = False
            returncode = 0

        result = CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=write_commandline,
            returncode=returncode,
            has_error=has_error,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )
        if not has_error and changed:
            result.fixed_files = _changed_files(digests_before, digests_after)
        return result

    # 通常モード: Step1 （check） → Step2 (format)
    # Step1はread-onlyのため内容変化なし。変化検知のためStep1前にスナップショットを取る。
    # （他formatterのdigests_beforeと同じ起点で取る方針に揃える）
    taplo_digests_before = _snapshot_file_digests(targets)
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    check_proc = _run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    check_rc = check_proc.returncode

    if check_rc == 0:
        # 整形不要
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )

    # Step2: 書き込み
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    write_proc = _run_subprocess(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    has_error = write_proc.returncode != 0
    returncode = write_proc.returncode if has_error else 1

    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=check_proc.stdout.strip() if not has_error else output,
        elapsed=elapsed,
    )
    if not has_error:
        taplo_digests_after = _snapshot_file_digests(targets)
        changed = taplo_digests_after != taplo_digests_before
        if changed:
            result.fixed_files = _changed_files(taplo_digests_before, taplo_digests_after)
    return result


def _execute_shfmt_two_step(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_mode: bool,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Shfmtの2段階実行 （shfmt -l → shfmt -w）。

    prettierと同様、確認用引数 （-l） と書き込み用引数 （-w） が排他のため専用経路で処理する。

    通常モード （fix_mode=False）:

    - Step1: `prefix + args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded （整形不要）
    - Step1 rc != 0 → Step2 `prefix + args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted （整形成功）
      - Step2 rc != 0 → failed

    fixモード （fix_mode=True）:

    - Step1をスキップし、直接write-args付きで実行
    -内容ハッシュスナップショットで書き込みを検知
    """
    common_args: list[str] = list(config[f"{command}-args"])
    check_args: list[str] = list(config[f"{command}-check-args"])
    write_args: list[str] = list(config[f"{command}-write-args"])
    target_strs = [str(t) for t in targets]

    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]

    if fix_mode:
        digests_before = _snapshot_file_digests(targets)
        if args.verbose and on_output is not None:
            on_output(f"commandline: {shlex.join(write_commandline)}\n")
        write_proc = _run_subprocess(
            write_commandline,
            env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
        write_rc = write_proc.returncode
        output = write_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        digests_after = _snapshot_file_digests(targets)
        changed = digests_after != digests_before

        if write_rc != 0:
            has_error = True
            returncode: int = write_rc
        elif changed:
            has_error = False
            returncode = 1
        else:
            has_error = False
            returncode = 0

        result = CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=write_commandline,
            returncode=returncode,
            has_error=has_error,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )
        if not has_error and changed:
            result.fixed_files = _changed_files(digests_before, digests_after)
        return result

    # 通常モード: Step1 （check） → Step2 (write)
    # Step1はread-onlyのため内容変化なし。変化検知のためStep1前にスナップショットを取る。
    # （他formatterのdigests_beforeと同じ起点で取る方針に揃える）
    shfmt_digests_before = _snapshot_file_digests(targets)
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    check_proc = _run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    check_rc = check_proc.returncode

    if check_rc == 0:
        # 整形不要
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )

    # Step2: 書き込み
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    write_proc = _run_subprocess(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    has_error = write_proc.returncode != 0
    returncode = write_proc.returncode if has_error else 1

    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=check_proc.stdout.strip() if not has_error else output,
        elapsed=elapsed,
    )
    if not has_error:
        shfmt_digests_after = _snapshot_file_digests(targets)
        changed = shfmt_digests_after != shfmt_digests_before
        if changed:
            result.fixed_files = _changed_files(shfmt_digests_before, shfmt_digests_after)
    return result


def _execute_prettier_two_step(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_mode: bool,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """Prettierの2段階実行 （prettier --check → prettier --write）。

    `prettier --check` （read-only） と `prettier --write` （書き込み） は排他のため、
    既存のautoflake/isort/blackの「同じ引数に--checkを付与する」ダンスは使えない。
    本ヘルパーでは以下のとおり実行する。

    通常モード （fix_mode=False）:

    - Step1: `prefix + args + check-args + additional + targets`を実行
    - Step1 rc == 0 → succeeded （書き込み不要）
    - Step1 rc == 1 → Step2 `prefix + args + write-args + additional + targets`を実行
      - Step2 rc == 0 → formatted （書き込み成功）
      - Step2 rc != 0 → failed
    - Step1 rc >= 2 → failed （設定ミス等）

    fixモード （fix_mode=True）:

    - Step1はスキップし、直接 `prefix + args + write-args + additional + targets`を実行
    -書き込み検知には内容ハッシュスナップショットを使う
    - rc != 0 → failed
    - rc == 0かつハッシュ変化あり → formatted
    - rc == 0かつ変化なし → succeeded
    """
    common_args: list[str] = list(config[f"{command}-args"])
    check_args: list[str] = list(config[f"{command}-check-args"])
    write_args: list[str] = list(config[f"{command}-write-args"])
    target_strs = [str(t) for t in targets]

    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]

    if fix_mode:
        digests_before = _snapshot_file_digests(targets)
        if args.verbose and on_output is not None:
            on_output(f"commandline: {shlex.join(write_commandline)}\n")
        write_proc = _run_subprocess(
            write_commandline,
            env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
        write_rc = write_proc.returncode
        output = write_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        digests_after = _snapshot_file_digests(targets)
        changed = digests_after != digests_before

        if write_rc != 0:
            has_error = True
            returncode: int = write_rc
            result_command_type: str = command_info.type
        elif changed:
            has_error = False
            returncode = 1
            result_command_type = "formatter"
        else:
            has_error = False
            returncode = 0
            result_command_type = command_info.type

        errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
        result = CommandResult.from_run(
            command=command,
            command_type=result_command_type,
            commandline=write_commandline,
            returncode=returncode,
            has_error=has_error,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )
        if not has_error and changed:
            result.fixed_files = _changed_files(digests_before, digests_after)
        return result

    # 通常モード: Step1 （check） → 必要ならStep2 （write）
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = _run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode

    if step1_rc == 0:
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=0,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )

    if step1_rc >= 2:
        # 設定ミス等の致命的エラー。Step2は実行しない。
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
        return CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=check_commandline,
            returncode=step1_rc,
            has_error=True,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )

    # Step1 rc == 1 → Step2実行 （書き込み）
    prettier_digests_before = _snapshot_file_digests(targets)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    step2_proc = _run_subprocess(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step2_rc = step2_proc.returncode
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    if step2_rc == 0:
        has_error = False
        returncode = 1  # formatted扱い
    else:
        has_error = True
        returncode = step2_rc

    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
    result = CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )
    if not has_error:
        prettier_digests_after = _snapshot_file_digests(targets)
        changed = prettier_digests_after != prettier_digests_before
        if changed:
            result.fixed_files = _changed_files(prettier_digests_before, prettier_digests_after)
    return result


def _snapshot_file_digests(targets: list[pathlib.Path]) -> dict[pathlib.Path, bytes]:
    """対象ファイルの内容ハッシュ （BLAKE2b） スナップショットを取得。

    mtimeベースの比較はtextlint --fixのように「残存違反がなくても
    ファイルを書き戻す」ツールで偽陽性を起こすため、内容ハッシュで比較する。
    ファイルが存在しない場合は空bytesを設定する （比較で差分検知できる）。
    """
    result: dict[pathlib.Path, bytes] = {}
    for target in targets:
        try:
            with target.open("rb") as f:
                result[target] = hashlib.file_digest(f, "blake2b").digest()
        except OSError:
            result[target] = b""
    return result


def _changed_files(
    before: dict[pathlib.Path, bytes],
    after: dict[pathlib.Path, bytes],
) -> list[str]:
    """ハッシュスナップショット前後で内容が変化したファイルのパス文字列リストを返す。

    `_snapshot_file_digests` の戻り値を2点渡し、ハッシュが変化したキーを抽出する。
    結果は文字列化してソートして返す（summary.applied_fixesの安定ソート用）。
    """
    return sorted(str(p) for p, digest in after.items() if before.get(p) != digest)


def _snapshot_file_texts(targets: list[pathlib.Path]) -> dict[pathlib.Path, str]:
    """対象ファイルのテキスト内容スナップショットを取得する。

    textlint fixの保護対象識別子破損検知に使う。読み込めないファイルは辞書から
    除外する （比較時には「前後どちらにも出現しない」と解釈される）。
    """
    result: dict[pathlib.Path, str] = {}
    for target in targets:
        try:
            result[target] = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return result


def _warn_protected_identifier_corruption(
    before: dict[pathlib.Path, str],
    after: dict[pathlib.Path, str],
    protected_identifiers: list[str],
) -> None:
    """Textlint fix後に保護対象識別子が失われていた場合、警告を発行する。

    fix前のファイル内容に含まれていた識別子がfix後に1件でも減っていれば、
    当該識別子が `preset-jtf-style` などの機械変換で破損した可能性が高い。
    検知は出現回数ベース （等号比較） で行い、単純な減少も破損として扱う。
    """
    for path, before_text in before.items():
        after_text = after.get(path)
        if after_text is None:
            continue
        if before_text == after_text:
            continue  # 変化なしの場合は検査不要
        for identifier in protected_identifiers:
            before_count = before_text.count(identifier)
            after_count = after_text.count(identifier)
            if before_count > after_count:
                pyfltr.warnings_.emit_warning(
                    source="textlint-identifier-corruption",
                    message=(
                        f"textlint fix が保護対象識別子を変換した可能性: "
                        f"{identifier!r} (file={pyfltr.paths.to_cwd_relative(path)}, "
                        f"before={before_count}, after={after_count})"
                    ),
                    hint="保護したい識別子はバックティックで囲むとtextlintのfixで改変されなくなる",
                )


def expand_all_files(targets: list[pathlib.Path], config: pyfltr.config.Config) -> list[pathlib.Path]:
    """対象ファイルの一括展開。

    ディレクトリ走査・excludeチェック・gitignoreフィルタリングを1回だけ実行し、
    全ファイルのリストを返す。コマンドごとのglobフィルタリングはfilter_by_globsで行う。
    """
    # 空ならカレントディレクトリを対象とする
    if len(targets) == 0:
        targets = [pathlib.Path(".")]

    # コマンドラインで直接指定されたファイル（ディレクトリでないもの）を記録
    directly_specified: set[pathlib.Path] = set()
    expanded: list[pathlib.Path] = []

    def _expand_target(target: pathlib.Path, *, is_direct: bool) -> None:
        try:
            match = excluded(target, config)
            if match is not None:
                if is_direct:
                    key, pattern = match
                    pyfltr.warnings_.emit_warning(
                        source="file-resolver",
                        message=(f'指定されたファイルが除外設定により無視されました: {target} ({key}="{pattern}" による)'),
                    )
                    pyfltr.warnings_.add_excluded_direct_file(str(target))
                return
            if target.is_dir():
                for child in target.iterdir():
                    _expand_target(child, is_direct=False)
            else:
                expanded.append(target)
                if is_direct:
                    directly_specified.add(target)
        except OSError:
            pyfltr.warnings_.emit_warning(
                source="file-resolver",
                message=f"I/O Error: {target}",
                exc_info=True,
            )

    for target in targets:
        # 絶対パスの場合はcwd基準の相対パスに変換
        if target.is_absolute():
            with contextlib.suppress(ValueError):
                target = target.relative_to(pathlib.Path.cwd())
        is_direct = not target.is_dir()
        _expand_target(target, is_direct=is_direct)

    # .gitignoreフィルタリング
    if config["respect-gitignore"]:
        before_gitignore = set(expanded)
        expanded = _filter_by_gitignore(expanded)
        # 直接指定されたファイルがgitignoreで除外された場合に警告
        after_set = set(expanded)
        for target in directly_specified:
            if target in before_gitignore and target not in after_set:
                pyfltr.warnings_.emit_warning(
                    source="file-resolver",
                    message=f"指定されたファイルが .gitignore により無視されました: {target}",
                )
                pyfltr.warnings_.add_excluded_direct_file(str(target))

    return expanded


def _filter_by_gitignore(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """Git check-ignoreで .gitignoreに該当するファイルを除外する。"""
    if not paths:
        return paths
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(str(p) for p in paths),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        pyfltr.warnings_.emit_warning(source="git", message="git が見つからないため respect-gitignore をスキップする")
        return paths
    except subprocess.TimeoutExpired:
        pyfltr.warnings_.emit_warning(source="git", message="git check-ignore がタイムアウトしたためスキップする")
        return paths
    if result.returncode not in (0, 1):
        # 0: 1つ以上ignored, 1: 全てnot ignored, 128: fatal error（リポジトリ外等）
        logger.debug("git check-ignore が終了コード %d を返した", result.returncode)
        return paths
    ignored_set: set[str] = set()
    if result.stdout:
        ignored_set = {s for s in result.stdout.split("\0") if s}
    return [p for p in paths if str(p) not in ignored_set]


def filter_by_globs(all_files: list[pathlib.Path], globs: list[str]) -> list[pathlib.Path]:
    """ファイルリストをglobパターンでフィルタリングする。"""
    return [f for f in all_files if any(f.match(glob) for glob in globs)]


def filter_by_changed_since(all_files: list[pathlib.Path], ref: str) -> list[pathlib.Path]:
    """`--changed-since <ref>` で変更ファイルに絞り込む。

    `git diff --name-only <ref>` でコミット差分とtrackedファイルの作業ツリー差分・staged差分の
    和集合を取得し、`all_files` との交差を返す。
    untracked（`git add` 未実施の新規ファイル）は対象外となる。

    git不在またはrefが存在しない場合は警告を出して `all_files` をそのまま返す（全体実行へフォールバック）。
    """
    changed = _get_changed_files(ref)
    if changed is None:
        return all_files
    if not changed:
        return []
    # normalize_separatorsを使って区切り文字を統一してから比較する。
    # all_filesはcwd起点の相対Pathであり、git diffもcwd起点の相対パスを返す。
    changed_norm: set[str] = {pyfltr.paths.normalize_separators(p) for p in changed}
    return [f for f in all_files if pyfltr.paths.normalize_separators(f) in changed_norm]


def _get_changed_files(ref: str) -> list[str] | None:
    """`git diff --name-only <ref>` でコミット差分とtrackedファイルの作業ツリー差分・staged差分を取得する。

    untracked（`git add` 未実施の新規ファイル）は `git diff` の出力に含まれないため対象外となる。
    成功時はパス文字列のリストを返す。git不在・ref不在・タイムアウト時は
    警告を出して `None` を返す（呼び出し元が全体実行へフォールバックする）。
    """
    # HEADからのコミット差分（<ref>..HEAD）とtrackedファイルの作業ツリー差分・staged差分の
    # 3種を「git diff <ref>」1コマンドで取得する。
    # git diff <ref> は <ref> と作業ツリーの差分（stagedも含む）を返すため、
    # コミット間差分 + trackedファイルの作業ツリー差分を一度に網羅できる。
    # 出力は-zオプションでNUL区切りにしてパスにスペースや特殊文字が含まれるケースに対応する。
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"git が見つからないため --changed-since={ref!r} をスキップして全体実行します",
        )
        return None
    except subprocess.TimeoutExpired:
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"git diff --name-only がタイムアウトしたため --changed-since={ref!r} をスキップして全体実行します",
        )
        return None
    if result.returncode != 0:
        # 終了コード非0はref不在・リポジトリ外などを示す。
        stderr_msg = result.stderr.strip()
        detail = f": {stderr_msg}" if stderr_msg else ""
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"--changed-since={ref!r} の ref が解決できないためスキップして全体実行します{detail}",
        )
        return None
    # NUL区切りでパースし、空文字列エントリを除去する。
    return [p for p in result.stdout.split("\0") if p]


def _matches_exclude_patterns(path: pathlib.Path, patterns: list[str]) -> str | None:
    """パスが除外パターンのいずれかに一致した場合、最初に一致したパターン文字列を返す。"""
    for glob in patterns:
        if path.match(glob):
            return glob
    # 親ディレクトリに一致しても可
    part = path.parent
    for _ in range(len(path.parts) - 1):
        for glob in patterns:
            if part.match(glob):
                return glob
        part = part.parent
    return None


def excluded(path: pathlib.Path, config: pyfltr.config.Config) -> tuple[str, str] | None:
    """無視パターンチェック。一致した場合は（設定キー名, 一致パターン）を、無一致の場合はNoneを返す。"""
    for key in ("exclude", "extend-exclude"):
        matched = _matches_exclude_patterns(path, config[key])
        if matched is not None:
            return (key, matched)
    return None
