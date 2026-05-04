"""runner解決とコマンドライン構築。"""

import dataclasses
import functools
import os
import pathlib
import shutil

import pyfltr.command.mise
import pyfltr.config.config
from pyfltr.command.builtin import COMMAND_RUNNERS, JS_RUNNERS

# `build_mise_subprocess_env`はpyfltr.command内部APIだがサブパッケージ全域で共有する。
# 同じサブパッケージ内の`mise.py`もfrom-importで取り込んでおり、本モジュールも倣う。
from pyfltr.command.env import build_mise_subprocess_env

logger = __import__("logging").getLogger(__name__)


# pyfltrのコマンド名 -> 実際に起動するパッケージのbin名の対応表。
# markdownlintコマンドは実体がmarkdownlint-cli2である点に注意。
JS_TOOL_BIN: dict[str, str] = {
    "textlint": "textlint",
    "markdownlint": "markdownlint-cli2",
    "eslint": "eslint",
    "prettier": "prettier",
    "biome": "biome",
    "vitest": "vitest",
    "oxlint": "oxlint",
    "tsc": "tsc",
}

# pyfltrのコマンド名 -> uv経由およびdirect経路で起動する実行ファイル名。
# ruff-format / ruff-check は実行ファイル名が `ruff` なので別名解決が必要。
PYTHON_TOOL_BIN: dict[str, str] = {
    "ruff-format": "ruff",
    "ruff-check": "ruff",
    "mypy": "mypy",
    "pylint": "pylint",
    "pyright": "pyright",
    "ty": "ty",
    "pytest": "pytest",
    "uv-sort": "uv-sort",
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
class StructuredOutputSpec:
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
_STRUCTURED_OUTPUT_SPECS: dict[str, tuple[str, StructuredOutputSpec]] = {
    "ruff-check-json": (
        "ruff-check",
        StructuredOutputSpec(
            inject=["--output-format=json"],
            conflicts=["--output-format"],
        ),
    ),
    "pylint-json": (
        "pylint",
        StructuredOutputSpec(
            inject=["--output-format=json2"],
            conflicts=["--output-format"],
        ),
    ),
    "pyright-json": (
        "pyright",
        StructuredOutputSpec(
            inject=["--outputjson"],
            conflicts=["--outputjson"],
        ),
    ),
    "pytest-tb-line": (
        "pytest",
        StructuredOutputSpec(
            inject=["--tb=short"],
            conflicts=["--tb"],
        ),
    ),
    "shellcheck-json": (
        "shellcheck",
        StructuredOutputSpec(
            inject=["-f", "json"],
            conflicts=["-f"],
        ),
    ),
    "textlint-json": (
        "textlint",
        StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
            lint_only=True,
        ),
    ),
    "typos-json": (
        "typos",
        StructuredOutputSpec(
            inject=["--format=json"],
            conflicts=["--format"],
        ),
    ),
    "eslint-json": (
        "eslint",
        StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
        ),
    ),
    "biome-json": (
        "biome",
        StructuredOutputSpec(
            inject=["--reporter=github"],
            conflicts=["--reporter"],
        ),
    ),
}


def get_structured_output_spec(command: str, config: pyfltr.config.config.Config) -> StructuredOutputSpec | None:
    """コマンドに対応する構造化出力仕様を返す。無効化されていればNone。"""
    for config_key, entry in _STRUCTURED_OUTPUT_SPECS.items():
        cmd = entry[0]
        spec = entry[1]
        if cmd == command and config.values.get(config_key, False):
            return spec
    return None


def _apply_structured_output(commandline: list[str], spec: StructuredOutputSpec) -> list[str]:
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
    （`"direct"` / `"mise"` / `"uv"` / `"uvx"` / `"pnpx"` / `"pnpm"` / `"npm"` / `"npx"` / `"yarn"`）。
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


def resolve_runner(command: str, config: pyfltr.config.config.Config) -> tuple[str, str]:
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
    default_runner = pyfltr.config.config.DEFAULT_CONFIG.get(f"{command}-runner")
    source = "default" if runner == default_runner else "explicit"
    return str(runner), source


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
    config: pyfltr.config.config.Config,
    *,
    allow_side_effects: bool,
) -> bool:
    """mise設定で当該ツールが活性化されているかを判定する。

    判定キーは `spec.mise_backend or spec.bin_name`（mise.toml記述に合わせた形）。
    例えばcargo系なら `rust`、cargo-denyなら `aqua:EmbarkStudios/cargo-deny`、
    その他のシンプル系（actionlint等）は `bin_name` でそのまま引く。

    `get_mise_active_tools` のキャッシュ・フォールバック挙動を利用するため、
    取得失敗時は自然に `False`（記述なし扱い）が返り、tool spec省略を発動しない。
    """
    del command  # 現状判定にコマンド名は使わない（specキーで一意）。引数は将来拡張余地のため残す。
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=allow_side_effects)
    key = spec.mise_backend or spec.bin_name
    return key in result.tools


@functools.lru_cache(maxsize=1)
def ensure_uv_available() -> bool:
    """`uv` バイナリが PATH 上に存在するかを判定する（実行内キャッシュつき）。

    `{command}-runner = "uv"` 経路で `uv run --frozen <bin>` を組み立てるかどうかの判定に使う。
    未導入時は False を返し、呼び出し側が direct フォールバックへ切り替える。エラー送出はしない。
    """
    return shutil.which("uv") is not None


@functools.lru_cache(maxsize=1)
def ensure_uvx_available() -> bool:
    """`uvx` shim が PATH 上に存在するかを判定する（実行内キャッシュつき）。

    `{command}-runner = "uvx"` 経路で `uvx <bin>` を組み立てるかどうかの判定に使う。
    未導入時は False を返し、呼び出し側が direct フォールバックへ切り替える。エラー送出はしない。
    """
    return shutil.which("uvx") is not None


@functools.lru_cache(maxsize=1)
def cwd_has_uv_lock() -> bool:
    """カレントディレクトリに `uv.lock` が存在するかを判定する（実行内キャッシュつき）。"""
    return pathlib.Path("uv.lock").is_file()


def _resolve_python_tool_direct(command: str) -> str:
    """Python系ツールのdirect経路実行ファイル解決。

    `PYTHON_TOOL_BIN[command]` を `shutil.which` で絶対パスへ解決する。
    解決不能なら `FileNotFoundError` を送出する。
    `{command}-runner = "uv"` がdirectフォールバックする経路と、
    利用者が `mypy-runner = "direct"` 等を明示した経路で共有する。
    """
    bin_name = PYTHON_TOOL_BIN[command]
    resolved = shutil.which(bin_name)
    if resolved is None:
        raise FileNotFoundError(bin_name)
    return resolved


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


def _resolve_python_commandline(
    command: str,
    effective: str,
) -> tuple[str, str, list[str]]:
    """Python系ツールの実行ファイルと引数prefixを決定する。

    `effective` には `python-runner` 委譲解決後の値（`"direct"` / `"uv"` / `"uvx"`）が入る。
    `(effective_runner, executable, prefix)` の3要素タプルを返す。
    `effective_runner`は `direct` フォールバックを反映した最終形となる。

    - `"uv"`: cwdに`uv.lock`があり、かつ`uv`バイナリが利用可能な場合は `uv run --frozen <bin>` を組み立てる。
      いずれかが満たされなければ direct フォールバック
    - `"uvx"`: `uvx`shimが利用可能なら `uvx <bin>` を組み立てる。
      未導入ならdirect フォールバック。`uv.lock`は参照しない、`{command}-version`設定とも連動しない
    - `"direct"`: `_resolve_python_tool_direct` で `shutil.which` 解決
    """
    bin_name = PYTHON_TOOL_BIN[command]
    if effective == "uv":
        if cwd_has_uv_lock() and ensure_uv_available():
            return "uv", "uv", ["run", "--frozen", bin_name]
        # uv不在 or uv.lock不在 → direct PATH解決へフォールバック。
        executable = _resolve_python_tool_direct(command)
        return "direct", executable, []
    if effective == "uvx":
        if ensure_uvx_available():
            return "uvx", "uvx", [bin_name]
        # uvx不在 → direct PATH解決へフォールバック。
        executable = _resolve_python_tool_direct(command)
        return "direct", executable, []
    if effective == "direct":
        executable = _resolve_python_tool_direct(command)
        return "direct", executable, []
    raise ValueError(f"python-runnerの設定値が正しくありません: {effective=}")


def _resolve_js_commandline(
    command: str,
    config: pyfltr.config.config.Config,
    *,
    effective: str | None = None,
) -> tuple[str, list[str]]:
    """JSツール（textlint / markdownlint等）の実行ファイルと引数prefixを決定する。

    `{command}-path` が空のときに呼び出される。
    `effective` を明示すると当該値を採用し、省略時は `js-runner` 設定値を採用する
    （per-tool直接指定値（`pnpx` / `pnpm` 等）を委譲経路と同一ロジックで解決するため）。
    `direct` モードで `node_modules/.bin/<cmd>` が存在しない場合は `FileNotFoundError` を送出する。
    """
    bin_name = JS_TOOL_BIN[command]
    runner = effective if effective is not None else config["js-runner"]
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


def _resolve_bin_commandline(
    command: str,
    config: pyfltr.config.config.Config,
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


# `{command}-runner`値の体系をbuiltin.py側のtuple定数から派生させ、
# 二重定義による追従漏れを避けるためのSSOT。
# - 委譲値: `*-runner`サフィックス（同名のグローバル設定キーへ委譲）
# - 直接指定値: それ以外（`direct` / `mise` / `uv` / `uvx` / `pnpx` / `pnpm` / `npm` / `npx` / `yarn`）
_DELEGATE_RUNNER_VALUES: frozenset[str] = frozenset(v for v in COMMAND_RUNNERS if v.endswith("-runner"))
_DIRECT_RUNNER_VALUES: frozenset[str] = frozenset(COMMAND_RUNNERS) - _DELEGATE_RUNNER_VALUES


def resolve_effective_runner(command: str, runner: str, config: pyfltr.config.config.Config) -> str:
    """`{command}-runner` per-tool値からeffective値を解決する。

    カテゴリ委譲値（`python-runner` / `js-runner` / `bin-runner`）はグローバル設定値に置換し、
    直接指定値（`direct` / `mise` / `uv` / `uvx` / `pnpx` / ...）はそのまま返す。
    未知値は`ValueError`を送出する（`{command}-runner`バリデーションを通過していれば本経路に来ない）。
    """
    # 委譲値はサフィックス`-runner`がそのままグローバル設定キーに一致する設計のため、
    # `runner`値をそのまま`config`の引きキーとして使える。
    if runner in _DELEGATE_RUNNER_VALUES:
        return str(config[runner])
    if runner in _DIRECT_RUNNER_VALUES:
        return runner
    raise ValueError(f"{command}-runnerの設定値が正しくありません: {runner=}")


# JSランナーのeffective値集合（直接指定値・グローバル委譲後の値の両方を判定するため）。
# `JS_RUNNERS`は`("pnpx", "pnpm", "npm", "npx", "yarn", "direct")`で`direct`を含むが、
# JSパッケージマネージャー経路の判定では`direct`を除外する（`direct`は別分岐で処理する）。
_JS_EFFECTIVE_VALUES: frozenset[str] = frozenset(JS_RUNNERS) - {"direct"}


def build_commandline(
    command: str,
    config: pyfltr.config.config.Config,
    *,
    allow_side_effects: bool = False,
) -> ResolvedCommandline:
    """ツール起動コマンドラインを構築する（副作用は `allow_side_effects` で制御）。

    `{command}-runner` および `{command}-path` の設定に従い、`mise exec ... --` 形式・
    `pnpx --package ...` 形式・`uv run --frozen` 形式・`uvx <bin>` 形式・直接実行（PATH解決）のいずれかを返す。
    mise経路では `get_mise_active_tools` を引いて、mise設定（プロジェクト `mise.toml` ＋
    グローバル設定）に該当ツール記述があり、かつ `{command}-version` が既定値 `"latest"` の
    ときに限りtool spec部分を省略した `["exec", "--", <bin>]` 形を返す
    （miseがmise設定の解決済み内容、つまりcomponentsや固定バージョンをそのまま使えるようにするため）。

    `allow_side_effects=False`（既定）では `mise exec --version` の事前チェックや
    `mise trust` を行わない。判定関数 `get_mise_active_tools` も副作用OFFで呼び、
    未信頼config由来エラーを「記述なし」扱いとして従来形のtool spec組み立てへフォールバックする。
    `command-info` サブコマンドの `--check` 無し呼び出しから安全に呼べるようにするためである。
    `allow_side_effects=True` 時は判定経路でも `mise-auto-trust` 設定に従いtrust→再呼び出しを許可する。

    ツールが特定できない場合は `FileNotFoundError` を、
    `{command}-runner` 値の組み合わせ自体が不正な場合は `ValueError` を送出する。
    """
    runner, source = resolve_runner(command, config)
    effective = resolve_effective_runner(command, runner, config)

    # `{command}-path` が非空ならば、その値でdirect実行する（明示パス上書き）。
    # この経路では `{command}-runner` と未登録ツールの組み合わせ（例: `typos-runner = "uv"` ＋ `typos-path` 指定）
    # でもエラー扱いせずpath値を採用する。利用者が明示的にパスを示している以上、起動経路の整合性より
    # 利用者の意図を優先する判断。明示runner × 未登録ツール × path未指定の場合のみ後段の分岐でエラー化する。
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

    if effective in _JS_EFFECTIVE_VALUES:
        if command not in JS_TOOL_BIN:
            raise ValueError(
                f"{command}: js-runner 対応ツールではないため "
                f'`{command}-runner = "{runner}"`（解決後 "{effective}"）は指定できません'
            )
        executable, prefix = _resolve_js_commandline(command, config, effective=effective)
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )

    if effective in ("uv", "uvx"):
        if command not in PYTHON_TOOL_BIN:
            raise ValueError(
                f"{command}: PYTHON_TOOL_BINに登録されていないため "
                f'`{command}-runner = "{runner}"`（解決後 "{effective}"）は指定できません'
            )
        resolved_effective, executable, prefix = _resolve_python_commandline(command, effective)
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=resolved_effective,
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
    if command in PYTHON_TOOL_BIN:
        # python-runner経由（python-runner = "direct"）と直接指定（{command}-runner = "direct"）の両方を吸収する。
        executable = _resolve_python_tool_direct(command)
        return ResolvedCommandline(
            executable=executable,
            prefix=[],
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    if command in JS_TOOL_BIN:
        # JSツールのdirectはnode_modules/.bin/<cmd> 解決に委譲。
        executable, prefix = _resolve_js_commandline(command, config, effective="direct")
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    # bin/js/python いずれの登録テーブルにも該当しないツール（typos / yamllint や利用者カスタムコマンド）は、
    # コマンド名そのものをPATH解決して直接起動する。`{command}-path` 未指定でも `{command}-runner = "direct"` の
    # 利用者意図に沿ってフォールバックする経路。PATH解決失敗時は `_resolve_direct_executable` が
    # `FileNotFoundError` を送出する。
    executable = _resolve_direct_executable(command)
    return ResolvedCommandline(
        executable=executable,
        prefix=[],
        runner=runner,
        runner_source=source,
        effective_runner=effective,
    )


def ensure_mise_available(
    resolved: ResolvedCommandline,
    config: pyfltr.config.config.Config,
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
    # `prefix[1] == "--"` で両形態を判別し、`mise exec --version` 用argsとエラー文面を切り替える。
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
    mise_env = build_mise_subprocess_env(dict(os.environ))
    if has_tool_spec:
        check_args = ["mise", "exec", tool_spec, "--", bin_name, "--version"]
    else:
        check_args = ["mise", "exec", "--", bin_name, "--version"]
    returncode, _stdout, stderr, trust_failed = pyfltr.command.mise.run_mise_with_trust(
        check_args, mise_env, config, allow_side_effects=True
    )
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


def _build_auto_args(command: str, config: pyfltr.config.config.Config, user_args: list[str]) -> list[str]:
    """自動引数を構築する。

    AUTO_ARGSで定義されたフラグがTrueの場合、対応する引数を返す。
    ユーザーが *-argsやCLI引数で既に同じ文字列を指定している場合はスキップする。
    """
    auto_entries = pyfltr.config.config.AUTO_ARGS.get(command, [])
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


def build_invocation_argv(
    command: str,
    config: pyfltr.config.config.Config,
    commandline_prefix: list[str],
    additional_args: list[str],
    *,
    fix_stage: bool,
) -> list[str]:
    """対象ファイル抜きの起動argvを構築する共通ヘルパー。

    通常段では `[prefix] + auto_args + {command}-args + {command}-lint-args + additional_args`
    に構造化出力引数を適用した結果を返す。fix段では `{command}-fix-args` を結合する。
    fix-args未定義のコマンドではfix_stage=Trueでも通常段と同じargvを返す。

    textlintのfix段は `execute_textlint_fix` のStep1と同じ規則
    （`--format` ペアを除去したargs + fix-args、auto_args / 構造化出力引数なし）を適用する。
    実行本体（`_prepare_execution_params` / `execute_textlint_fix`）と
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
    structured_spec = get_structured_output_spec(command, config)
    if structured_spec is not None and not (structured_spec.lint_only and fix_args_value is not None):
        commandline = _apply_structured_output(commandline, structured_spec)
    return commandline
