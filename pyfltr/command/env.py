"""subprocess環境構築。"""

import os
import shutil
import typing

logger = __import__("logging").getLogger(__name__)


def get_env_path(env: dict[str, str]) -> str | None:
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
    両者を吸収して比較する。POSIXでは大文字小文字を保ったまま末尾スラッシュのみ除去する。
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


def build_mise_subprocess_env(env: dict[str, str]) -> dict[str, str]:
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


def build_subprocess_env(
    config: "typing.Any",
    command: str,
    *,
    via_mise: bool = False,
) -> dict[str, str]:
    """サブプロセス実行用の環境変数を構築。

    `config` は `pyfltr.config.config.Config` のインスタンスを渡す。
    `via_mise=True` の場合、PATHからmiseが注入したtoolパス（installs / dotnet-root /
    shims）を除外する。これは `mise exec` 経由のサブプロセスでmiseがtools解決を
    スキップしてPATH解決にフォールバックしてしまう挙動を防ぐための対症療法。
    詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
    """
    env = os.environ.copy()
    if via_mise:
        env = build_mise_subprocess_env(env)
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
