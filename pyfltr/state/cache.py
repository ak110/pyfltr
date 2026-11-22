import dataclasses
import datetime as dt
import hashlib
import importlib.metadata
import json
import logging
import os
import pathlib
import shutil
import typing

import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.diagnostics
import pyfltr.parsing.entry
import pyfltr.paths
import pyfltr.user_cache

logger = logging.getLogger(__name__)

_CACHE_DIRNAME = "cache"


@dataclasses.dataclass(frozen=True)
class CachePolicy:
    max_age_hours: int
    """保存期間の上限（時間）。0以下で期間軸の自動削除を無効化する。"""


class CacheStore:
    def __init__(self, cache_root: pathlib.Path | None = None) -> None:
        self._cache_root = cache_root if cache_root is not None else pyfltr.user_cache.default_cache_root()
        self._cache_dir = self._cache_root / _CACHE_DIRNAME

    @property
    def cache_dir(self) -> pathlib.Path:
        return self._cache_dir

    def compute_key(
        self,
        *,
        command: str,
        commandline: list[str],
        fix_stage: bool,
        structured_output: bool,
        target_files: list[pathlib.Path],
        config_files: list[pathlib.Path],
        target_base_cwd: pathlib.Path | None = None,
        subproject_cwd: pathlib.Path | None = None,
    ) -> str:
        hasher = hashlib.sha256()
        hasher.update(f"pyfltr-major={_pyfltr_major_version()}\n".encode())
        # JSONL / archiveスキーマを変えた際の一括無効化用識別子。
        # `pyproject.toml`の版数は`hatch-vcs`管理でMAJORを手動更新できないため、
        # 互換性を破棄する際はこの定数を更新してキャッシュを一括無効化する。
        hasher.update(b"schema=v6\n")
        hasher.update(f"command={command}\n".encode())
        hasher.update(f"fix_stage={int(fix_stage)}\n".encode())
        hasher.update(f"structured={int(structured_output)}\n".encode())
        if subproject_cwd is not None:
            hasher.update(f"subproject={os.path.realpath(str(subproject_cwd))}\n".encode())
        hasher.update(b"commandline:\n")
        for token in commandline:
            hasher.update(token.encode("utf-8"))
            hasher.update(b"\0")
        hasher.update(b"targets:\n")
        target_base = target_base_cwd if target_base_cwd is not None else pathlib.Path.cwd()
        for target in target_files:
            hasher.update(str(target).encode("utf-8"))
            hasher.update(b"\0")
            target_path = target if target.is_absolute() else target_base / target
            hasher.update(_file_sha256(target_path).encode("ascii"))
            hasher.update(b"\n")
        hasher.update(b"configs:\n")
        for config_path in config_files:
            hasher.update(str(config_path).encode("utf-8"))
            hasher.update(b"\0")
            hasher.update(_file_sha256(config_path).encode("ascii"))
            hasher.update(b"\n")
        return hasher.hexdigest()

    def get(self, command: str, key: str) -> pyfltr.command.core_.CommandResult | None:
        path = self._entry_path(command, key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("cache: 破損したエントリをスキップ: %s", path)
            return None
        return _deserialize_result(data)

    def put(self, command: str, key: str, result: pyfltr.command.core_.CommandResult, *, run_id: str | None) -> None:
        if run_id is None:
            return
        path = self._entry_path(command, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _serialize_result(result, source_run_id=run_id)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def cleanup(self, policy: CachePolicy) -> list[pathlib.Path]:
        if not self._cache_dir.exists():
            return []
        if policy.max_age_hours <= 0:
            return []
        now = dt.datetime.now(dt.UTC).timestamp()
        age_limit = policy.max_age_hours * 3600
        removed: list[pathlib.Path] = []
        for entry in self._cache_dir.rglob("*.json"):
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if now - mtime > age_limit:
                try:
                    entry.unlink()
                    removed.append(entry)
                except OSError:
                    continue
        return removed

    def clear(self) -> None:
        if self._cache_dir.exists():
            shutil.rmtree(self._cache_dir, ignore_errors=True)

    def _entry_path(self, command: str, key: str) -> pathlib.Path:
        return self._cache_dir / pyfltr.paths.sanitize_command_name(command) / f"{key}.json"


def cache_policy_from_config(config: pyfltr.config.model.Config) -> CachePolicy:
    return CachePolicy(max_age_hours=int(config.values.get("cache-max-age-hours", 12)))


def _pyfltr_major_version() -> str:
    try:
        version = importlib.metadata.version("pyfltr")
    except importlib.metadata.PackageNotFoundError:
        return "dev"
    return version.split(".", 1)[0]


def _file_sha256(path: pathlib.Path) -> str:
    try:
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return ""


def _serialize_result(result: pyfltr.command.core_.CommandResult, *, source_run_id: str) -> dict[str, typing.Any]:
    return {
        "source_run_id": source_run_id,
        "command": result.command,
        "command_type": result.command_type,
        "commandline": list(result.commandline),
        "returncode": result.returncode,
        "files": result.files,
        "output": result.output,
        "elapsed": result.elapsed,
        "errors": [_error_to_dict(e) for e in result.errors],
        "target_files": [str(p) for p in result.target_files],
    }


def _deserialize_result(data: dict[str, typing.Any]) -> pyfltr.command.core_.CommandResult:
    errors = [_dict_to_error(e) for e in data.get("errors", [])]
    target_files = [pathlib.Path(p) for p in data.get("target_files", [])]
    return pyfltr.command.core_.CommandResult(
        command=data["command"],
        command_type=data["command_type"],
        commandline=list(data["commandline"]),
        returncode=data["returncode"],
        files=data["files"],
        output=data["output"],
        elapsed=data["elapsed"],
        errors=errors,
        target_files=target_files,
        cached=True,
        cached_from=data.get("source_run_id"),
    )


def _error_to_dict(error: pyfltr.diagnostics.ErrorLocation) -> dict[str, typing.Any]:
    return {
        "command": error.command,
        "file": error.file,
        "line": error.line,
        "col": error.col,
        "rule": error.rule,
        "rule_url": error.rule_url,
        "severity": error.severity,
        "fix": error.fix,
        "hint": error.hint,
        "message": error.message,
    }


def _dict_to_error(data: dict[str, typing.Any]) -> pyfltr.diagnostics.ErrorLocation:
    return pyfltr.diagnostics.ErrorLocation(
        command=data["command"],
        file=data["file"],
        line=data["line"],
        col=data.get("col"),
        rule=data.get("rule"),
        rule_url=data.get("rule_url"),
        severity=data.get("severity"),
        fix=data.get("fix"),
        hint=data.get("hint"),
        message=data["message"],
    )
