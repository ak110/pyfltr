"""mise統合。"""

import dataclasses
import json
import os
import shutil
import subprocess
import typing

import pyfltr.config.config
from pyfltr.command.env import build_mise_subprocess_env

logger = __import__("logging").getLogger(__name__)


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
    取得失敗時は `tools` を空辞書とし、`detail` に短い手がかり（mise stderr冒頭等）を格納する。
    `command-info` 出力やJSONL header経由で利用者に状況を可視化する目的で使う。
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


def get_mise_active_tools(
    config: pyfltr.config.config.Config,
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
    config: pyfltr.config.config.Config,
    *,
    allow_side_effects: bool,
) -> MiseActiveToolsResult:
    """`mise ls --current --json` を実際に呼び出し、ステータス付き結果を返す。

    キャッシュ管理は呼び出し側 `get_mise_active_tools` が担当する純取得層。
    取得失敗時のフォールバック挙動は本関数docstringおよび `get_mise_active_tools` の
    `allow_side_effects` 説明と同義。
    """
    if shutil.which("mise") is None:
        return MiseActiveToolsResult(status="mise-not-found")
    mise_env = build_mise_subprocess_env(dict(os.environ))
    ls_args = ["mise", "ls", "--current", "--json"]
    return _run_mise_ls_with_trust_retry(ls_args, config, mise_env, allow_side_effects=allow_side_effects)


def run_mise_with_trust(
    args: list[str],
    mise_env: dict[str, str],
    config: pyfltr.config.config.Config,
    *,
    allow_side_effects: bool,
) -> tuple[int, str, str, bool]:
    """miseコマンドを実行し、未信頼エラー時はtrust試行→再実行する核ロジック。

    戻り値は `(returncode, stdout, stderr, trust_failed)` のタプル。
    `trust_failed=True` はtrust試行自体が失敗したことを示し、
    呼び出し側でエラーメッセージを切り替えるために使う。
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
    config: pyfltr.config.config.Config,
    mise_env: dict[str, str],
    *,
    allow_side_effects: bool,
) -> MiseActiveToolsResult:
    """`mise ls --current --json` を実行し、必要に応じてtrust試行→再実行する。

    成功時は `MiseActiveToolsResult(status="ok", tools=...)` を返す。
    失敗時（mise呼び出し失敗・JSONパース失敗・副作用OFF下の未信頼エラー・trust拒否）は
    対応するステータスを設定したMiseActiveToolsResultを返してフォールバックさせる。
    trust試行を含むリトライ核ロジックは `run_mise_with_trust` に委譲する。
    """
    try:
        returncode, stdout, stderr, trust_failed = run_mise_with_trust(
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
