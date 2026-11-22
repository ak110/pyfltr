import os
import shutil
import typing

import pyfltr.config.model

logger = __import__("logging").getLogger(__name__)


def get_env_path(env: dict[str, str]) -> str | None:
    if os.name == "nt":
        for key, value in env.items():
            if key.upper() == "PATH":
                return value
        return None
    return env.get("PATH")


_MISE_TOOL_PATH_MARKERS: tuple[str, ...] = (
    "/mise/installs/",
    "/mise/dotnet-root",
    "/mise/shims",
)


def _normalize_path_entry_for_dedup(entry: str) -> str:
    if os.name == "nt":
        return entry.replace("/", "\\").rstrip("\\").lower()
    return entry.rstrip("/")


def _detect_path_key(env: "typing.Mapping[str, str]") -> str | None:
    if os.name == "nt":
        for key in env:
            if key.upper() == "PATH":
                return key
        return None
    return "PATH" if "PATH" in env else None


def _dedupe_path_value(path_value: str) -> str:
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
    if entry == "":
        return False
    normalized = entry.replace("\\", "/")
    if os.name == "nt":
        normalized = normalized.lower()
    return any(marker in normalized for marker in _MISE_TOOL_PATH_MARKERS)


def _strip_mise_tool_paths(path_value: str) -> str:
    sep = os.pathsep
    return sep.join(entry for entry in path_value.split(sep) if not _is_mise_tool_path(entry))


def build_mise_subprocess_env(env: dict[str, str]) -> dict[str, str]:
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
    effective_runner: str | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    if via_mise:
        env = build_mise_subprocess_env(env)
    if effective_runner == "uv":
        env.pop("VIRTUAL_ENV", None)
    # サプライチェーン攻撃対策: パッケージ取得系ツールの最小待機期間を既定で設定する。
    # pnpmはnpm互換のconfig環境変数方式 （NPM_CONFIG_<SNAKE_CASE>） を採用する。
    env.setdefault("UV_EXCLUDE_NEWER", "1 day")
    env.setdefault("NPM_CONFIG_MINIMUM_RELEASE_AGE", "1440")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Windowsのcp932/cp1252などに依存せず、ツール側のopen()/Path.read_text() をUTF-8で動かす。
    # 例: uv-sortがpyproject.tomlをエンコーディング未指定で読み込む箇所で発生する
    # UnicodeDecodeErrorを回避する。
    env["PYTHONUTF8"] = "1"
    if pyfltr.config.model.command_setting(config.values, command, "devmode", False):
        env["PYTHONDEVMODE"] = "1"
    env["COLUMNS"] = str(min(max(shutil.get_terminal_size().columns - 4, 80), 128))
    return env
