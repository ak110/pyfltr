import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import typing

import pyfltr.config.config
import pyfltr.config.model
from pyfltr.command.env import build_mise_subprocess_env

logger = __import__("logging").getLogger(__name__)


MiseActiveToolsStatus = typing.Literal[
    "ok",
    "mise-not-found",
    "untrusted-no-side-effects",
    "trust-failed",
    "exec-error",
    "json-parse-error",
    "unexpected-shape",
]
"""`mise ls --current --json` 取得結果の状態スラッグ。SSOTは本定義。

- `ok`: 取得成功（dictは空でも `mise.toml` 記述が無いだけの場合は `ok`）
- `mise-not-found`: `mise` がPATH上に存在しない
- `untrusted-no-side-effects`: 未信頼config由来エラーで副作用OFFのためtrust試行を行わなかった
- `trust-failed`: trust試行が拒否された
- `exec-error`: その他のmise実行エラー（`OSError` 含む）
- `json-parse-error`: stdoutのJSONパースに失敗した
- `unexpected-shape`: dict以外の値が返された

ステータス追加・露出経路追加時は `docs/guide/usage.md` の `command-info` 節を併せて更新する。
"""


@dataclasses.dataclass(frozen=True)
class MiseActiveToolsResult:
    status: MiseActiveToolsStatus
    tools: dict[str, typing.Any] = dataclasses.field(default_factory=dict)
    detail: str | None = None


_MISE_ACTIVE_TOOLS_CACHE: dict[tuple[str, tuple[tuple[str, str | None], ...], bool], MiseActiveToolsResult] = {}

_MISE_CONFIG_ENV_KEYS: tuple[str, ...] = (
    "MISE_CONFIG_FILE",
    "MISE_ENV",
    "MISE_DEFAULT_CONFIG_FILENAME",
)


def _mise_env_signature() -> tuple[tuple[str, str | None], ...]:
    return tuple((key, os.environ.get(key)) for key in _MISE_CONFIG_ENV_KEYS)


def get_mise_active_tools(
    config: pyfltr.config.model.Config,
    *,
    allow_side_effects: bool = False,
    cwd: pathlib.Path | None = None,
) -> MiseActiveToolsResult:
    cwd_for_query = cwd if cwd is not None else pathlib.Path.cwd()
    cache_key = (os.path.realpath(str(cwd_for_query)), _mise_env_signature(), allow_side_effects)
    cached = _MISE_ACTIVE_TOOLS_CACHE.get(cache_key)
    if cached is not None:
        return cached

    result = _query_mise_active_tools(config, allow_side_effects=allow_side_effects, cwd=cwd_for_query)
    _MISE_ACTIVE_TOOLS_CACHE[cache_key] = result
    return result


def _query_mise_active_tools(
    config: pyfltr.config.model.Config,
    *,
    allow_side_effects: bool,
    cwd: pathlib.Path | None = None,
) -> MiseActiveToolsResult:
    if shutil.which("mise") is None:
        return MiseActiveToolsResult(status="mise-not-found")
    mise_env = build_mise_subprocess_env(dict(os.environ))
    ls_args = ["mise", "ls", "--current", "--json"]
    return _run_mise_ls_with_trust_retry(ls_args, config, mise_env, allow_side_effects=allow_side_effects, cwd=cwd)


def run_mise_with_trust(
    args: list[str],
    mise_env: dict[str, str],
    config: pyfltr.config.model.Config,
    *,
    allow_side_effects: bool,
    cwd: pathlib.Path | None = None,
) -> tuple[int, str, str, bool]:
    cwd_arg = str(cwd) if cwd is not None else None
    trusted = False
    while True:
        check = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            check=False,
            env=mise_env,
            cwd=cwd_arg,
        )
        if check.returncode == 0:
            return check.returncode, check.stdout, check.stderr, False
        stderr = check.stderr
        if not trusted and allow_side_effects and config["mise-auto-trust"] and "not trusted" in stderr:
            trust = subprocess.run(
                ["mise", "trust", "--yes", "--all"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
                check=False,
                env=mise_env,
                cwd=cwd_arg,
            )
            if trust.returncode == 0:
                trusted = True
                continue
            return trust.returncode, "", trust.stderr, True
        return check.returncode, check.stdout, check.stderr, False


def _run_mise_ls_with_trust_retry(
    ls_args: list[str],
    config: pyfltr.config.model.Config,
    mise_env: dict[str, str],
    *,
    allow_side_effects: bool,
    cwd: pathlib.Path | None = None,
) -> MiseActiveToolsResult:
    try:
        returncode, stdout, stderr, trust_failed = run_mise_with_trust(
            ls_args, mise_env, config, allow_side_effects=allow_side_effects, cwd=cwd
        )
    except OSError as e:
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
        return MiseActiveToolsResult(status="unexpected-shape", detail=f"got {type(parsed).__name__}")
    return MiseActiveToolsResult(status="ok", tools=parsed)


def _summarize_mise_detail(text: str, *, max_len: int = 200) -> str | None:
    flat = " ".join(text.split())
    if not flat:
        return None
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1].rstrip() + "…"
