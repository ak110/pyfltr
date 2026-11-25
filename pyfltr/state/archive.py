import contextlib
import dataclasses
import importlib.metadata
import json
import logging
import os
import pathlib
import posixpath
import re
import sys
import tempfile
import threading
import typing

import ulid

import pyfltr.command.core_
import pyfltr.config.model
import pyfltr.file_lock
import pyfltr.output.jsonl_records
import pyfltr.parsing.entry
import pyfltr.paths
import pyfltr.state.retention
import pyfltr.timestamps
import pyfltr.user_cache

logger = logging.getLogger(__name__)

_RUNS_DIRNAME = "runs"
_META_FILENAME = "meta.json"
_TOOL_OUTPUT_FILENAME = "output.log"
_TOOL_DIAGNOSTICS_FILENAME = "diagnostics.jsonl"
_TOOL_META_FILENAME = "tool.json"
TOOL_META_REMOVED_KEYS: frozenset[str] = frozenset({"has_error"})
"""保存済みのツールメタ情報から読み取り時に除去するフィールド。"""
DIAGNOSTIC_PATHS_META_KEY = "diagnostic_paths"
"""診断の`file`を起点相対で保存した版が`meta.json`へ書くキー。値は`DIAGNOSTIC_PATHS_START_RELATIVE`。

このキーを持たないrunは、サブプロジェクト相対の診断を保存していた版の記録として補正の対象にする。
"""
DIAGNOSTIC_PATHS_START_RELATIVE = "start-relative"
SLOT_WAITS_META_KEY = "slot_waits"
"""実行枠の待機区間の一覧を保存する`meta.json`のキー。キーの有無で計測した版の記録かを判別する。"""
_SECTION_HEADER_RE = re.compile(r"^# (?:subproject: (?P<relative>.+)|external paths)$")
_TRACKS_RUN_STATUS = "tracks_run_status"
RunStatus = typing.Literal["running", "completed", "interrupted", "unknown"]
InterruptionReason = typing.Literal["keyboard_interrupt", "mcp_cancelled"]


def generate_run_id() -> str:
    return str(ulid.ULID())


@dataclasses.dataclass(frozen=True)
class ArchivePolicy:
    max_runs: int
    """保存する最大世代数。"""
    max_size_bytes: int
    """アーカイブ全体の合計バイト数の上限。"""
    max_age_days: int
    """保存期間の上限 (日数)。"""


@dataclasses.dataclass(frozen=True)
class RunSummary:
    run_id: str
    started_at: str | None
    finished_at: str | None
    exit_code: int | None
    commands: list[str]
    files: int | None
    cwd: str | None
    """実行起点（`--work-dir`・MCPの`work_dir`適用後）。`meta.json`に文字列で記録されていない場合は`None`。"""
    status: RunStatus
    interruption_reason: InterruptionReason | None


class ArchiveStore:
    def __init__(self, cache_root: pathlib.Path | None = None) -> None:
        self._cache_root = cache_root if cache_root is not None else pyfltr.user_cache.default_cache_root()
        self._runs_dir = self._cache_root / _RUNS_DIRNAME
        self._run_locks: dict[str, pyfltr.file_lock.FileLock] = {}
        self._meta_lock = threading.Lock()

    @property
    def runs_dir(self) -> pathlib.Path:
        return self._runs_dir

    def start_run(
        self,
        *,
        run_id: str | None = None,
        commands: list[str] | None = None,
        files: int | None = None,
        cwd: str | None = None,
        argv: list[str] | None = None,
    ) -> str:
        run_id = run_id or generate_run_id()
        run_dir = self._runs_dir / run_id
        lock = pyfltr.file_lock.FileLock(_run_lock_path(run_dir))
        if not lock.try_acquire():
            raise FileExistsError(run_id)
        self._run_locks[run_id] = lock
        try:
            self._write_initial_meta(run_id, run_dir, commands=commands, files=files, cwd=cwd, argv=argv)
        except BaseException:
            self.release_run(run_id)
            raise
        return run_id

    def _write_initial_meta(
        self,
        run_id: str,
        run_dir: pathlib.Path,
        *,
        commands: list[str] | None,
        files: int | None,
        cwd: str | None,
        argv: list[str] | None,
    ) -> None:
        run_dir.mkdir()
        (run_dir / "tools").mkdir()
        meta = {
            "run_id": run_id,
            "version": importlib.metadata.version("pyfltr"),
            "python": sys.version,
            "executable": sys.executable,
            "platform": sys.platform,
            "cwd": cwd if cwd is not None else os.getcwd(),
            "argv": argv if argv is not None else sys.argv[1:],
            "commands": commands or [],
            "files": files,
            "started_at": pyfltr.timestamps.now_iso(),
            _TRACKS_RUN_STATUS: True,
            DIAGNOSTIC_PATHS_META_KEY: DIAGNOSTIC_PATHS_START_RELATIVE,
            SLOT_WAITS_META_KEY: [],
        }
        _write_json_replacing(run_dir / _META_FILENAME, meta)

    def write_tool_result(
        self,
        run_id: str,
        result: pyfltr.command.core_.CommandResult,
    ) -> None:
        tool_dir = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(result.command)
        tool_dir.mkdir(parents=True, exist_ok=True)
        (tool_dir / _TOOL_OUTPUT_FILENAME).write_text(result.output, encoding="utf-8")

        aggregated, hint_urls, hints = pyfltr.output.jsonl_records.aggregate_diagnostics(result.errors)
        with (tool_dir / _TOOL_DIAGNOSTICS_FILENAME).open("w", encoding="utf-8") as f:
            for record in aggregated:
                f.write(json.dumps(record, ensure_ascii=False))
                f.write("\n")
        meta: dict[str, typing.Any] = {
            "command": result.command,
            "type": result.command_type,
            "status": result.status,
            "returncode": result.returncode,
            "files": result.files,
            "elapsed": round(result.elapsed, 3),
            "diagnostics": len(result.errors),
            "commandline": result.commandline,
        }
        if hint_urls:
            meta["hint_urls"] = dict(hint_urls)
        if hints:
            meta["hints"] = dict(hints)
        if result.slow_tests:
            meta["slow_tests"] = [test.to_dict() for test in result.slow_tests]
        if result.retry_command is not None:
            meta["retry_command"] = result.retry_command
        if result.started_at is not None:
            meta["started_at"] = result.started_at
        if result.finished_at is not None:
            meta["finished_at"] = result.finished_at
        if result.launch_boundary_at is not None:
            meta["launch_boundary_at"] = result.launch_boundary_at
        (tool_dir / _TOOL_META_FILENAME).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_slot_wait(self, run_id: str, wait: dict[str, typing.Any]) -> None:

        def update(meta: dict[str, typing.Any]) -> bool:
            waits = meta.setdefault(SLOT_WAITS_META_KEY, [])
            key = (wait.get("kind"), wait.get("sequence"))
            for index, existing in enumerate(waits):
                if (existing.get("kind"), existing.get("sequence")) == key:
                    waits[index] = dict(wait)
                    return True
            waits.append(dict(wait))
            return True

        self._update_meta(run_id, update)

    def finalize_run(
        self,
        run_id: str,
        *,
        exit_code: int,
        commands: list[str] | None = None,
        files: int | None = None,
    ) -> None:
        try:
            self._write_final_meta(run_id, exit_code=exit_code, commands=commands, files=files)
        finally:
            self.release_run(run_id)

    def release_run(self, run_id: str) -> None:
        lock = self._run_locks.pop(run_id, None)
        if lock is not None:
            lock.release(remove=True)

    def record_interruption(self, run_id: str, reason: InterruptionReason, *, finished_at: str) -> None:
        def update(meta: dict[str, typing.Any]) -> bool:
            if not meta.get(_TRACKS_RUN_STATUS) or meta.get("finished_at") is not None or meta.get("exit_code") is not None:
                return False
            meta["finished_at"] = finished_at
            meta["interruption_reason"] = reason
            return True

        self._update_meta(run_id, update)

    def _write_final_meta(
        self,
        run_id: str,
        *,
        exit_code: int,
        commands: list[str] | None,
        files: int | None,
    ) -> None:
        def update(meta: dict[str, typing.Any]) -> bool:
            meta["finished_at"] = pyfltr.timestamps.now_iso()
            meta["exit_code"] = exit_code
            if commands is not None:
                meta["commands"] = commands
            if files is not None:
                meta["files"] = files
            return True

        self._update_meta(run_id, update)

    def _update_meta(self, run_id: str, update: typing.Callable[[dict[str, typing.Any]], bool]) -> None:
        meta_path = self._runs_dir / run_id / _META_FILENAME
        with self._meta_lock:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return
            if update(meta):
                _write_json_replacing(meta_path, meta)

    def list_runs(self, *, limit: int | None = None) -> list[RunSummary]:
        if not self._runs_dir.exists():
            return []
        entries = sorted(
            (entry for entry in self._runs_dir.iterdir() if entry.is_dir()),
            key=lambda p: p.name,
            reverse=True,
        )
        if limit is not None:
            entries = entries[:limit]
        summaries: list[RunSummary] = []
        for entry in entries:
            try:
                meta = self.read_meta(entry.name)
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                logger.debug("archive: 読み取れない meta.json をスキップ: %s", entry / _META_FILENAME)
                continue
            summaries.append(
                RunSummary(
                    run_id=entry.name,
                    started_at=meta.get("started_at"),
                    finished_at=meta.get("finished_at"),
                    exit_code=meta.get("exit_code"),
                    commands=list(meta.get("commands", [])),
                    files=meta.get("files"),
                    cwd=meta["cwd"] if isinstance(meta.get("cwd"), str) else None,
                    status=meta["status"],
                    interruption_reason=meta.get("interruption_reason"),
                )
            )
        return summaries

    def read_meta(self, run_id: str) -> dict[str, typing.Any]:
        run_dir = self._runs_dir / run_id
        meta_path = run_dir / _META_FILENAME
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        status = _terminal_status(meta)
        if status is None and meta.get(_TRACKS_RUN_STATUS):
            locked = pyfltr.file_lock.probe_lock(_run_lock_path(run_dir))
            if locked is False:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                status = _terminal_status(meta)
            elif locked is True:
                status = "running"
        meta["status"] = status or "unknown"
        meta.pop(_TRACKS_RUN_STATUS, None)
        return meta

    def list_tools(self, run_id: str) -> list[str]:
        tools_dir = self._runs_dir / run_id / "tools"
        if not tools_dir.exists():
            raise FileNotFoundError(run_id)
        return sorted(entry.name for entry in tools_dir.iterdir() if entry.is_dir())

    def read_tool_meta(self, run_id: str, tool: str) -> dict[str, typing.Any]:
        path = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(tool) / _TOOL_META_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"{run_id}/{tool}")
        meta = json.loads(path.read_text(encoding="utf-8"))
        return {key: value for key, value in meta.items() if key not in TOOL_META_REMOVED_KEYS}

    def read_tool_output(self, run_id: str, tool: str) -> str:
        path = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(tool) / _TOOL_OUTPUT_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"{run_id}/{tool}")
        return path.read_text(encoding="utf-8")

    def read_tool_diagnostics(self, run_id: str, tool: str) -> list[dict[str, typing.Any]]:
        path = self._runs_dir / run_id / "tools" / pyfltr.paths.sanitize_command_name(tool) / _TOOL_DIAGNOSTICS_FILENAME
        if not path.exists():
            raise FileNotFoundError(f"{run_id}/{tool}")
        entries: list[dict[str, typing.Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            entries.append(json.loads(line))
        if self._saved_subproject_relative_diagnostics(run_id):
            try:
                output = self.read_tool_output(run_id, tool)
            except FileNotFoundError:
                return entries
            entries = restore_subproject_paths(tool, output, entries)
        return entries

    def _saved_subproject_relative_diagnostics(self, run_id: str) -> bool:
        try:
            meta = self.read_meta(run_id)
        except (FileNotFoundError, ValueError):
            return False
        return meta.get(DIAGNOSTIC_PATHS_META_KEY) != DIAGNOSTIC_PATHS_START_RELATIVE

    def cleanup(self, policy: ArchivePolicy) -> list[str]:
        retention_policy = pyfltr.state.retention.RetentionPolicy(
            max_entries=policy.max_runs,
            max_size_bytes=policy.max_size_bytes,
            max_age_days=policy.max_age_days,
        )
        removed = pyfltr.state.retention.cleanup_generational_directory(
            self._runs_dir,
            retention_policy,
            meta_filename=_META_FILENAME,
            timestamp_key="finished_at",
            fallback_timestamp_key="started_at",
            is_active=lambda run_dir: pyfltr.file_lock.probe_lock(_run_lock_path(run_dir)) is not False,
        )
        for run_id in removed:
            with contextlib.suppress(PermissionError):
                _run_lock_path(self._runs_dir / run_id).unlink(missing_ok=True)
        return removed


def _terminal_status(meta: dict[str, typing.Any]) -> RunStatus | None:
    if not isinstance(meta.get("finished_at"), str):
        return None
    exit_code = meta.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        return "completed"
    if meta.get(_TRACKS_RUN_STATUS) and meta.get("interruption_reason") in ("keyboard_interrupt", "mcp_cancelled"):
        return "interrupted"
    return None


def _write_json_replacing(path: pathlib.Path, data: dict[str, typing.Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False) as file:
        file.write(text)
        temp_path = pathlib.Path(file.name)
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _run_lock_path(run_dir: pathlib.Path) -> pathlib.Path:
    return run_dir.with_name(f"{run_dir.name}.lock")


_PYTEST_APPENDED_LOCATION_RE = re.compile(r"(?P<message>.*) \((?P<file>\S+):(?P<line>\d+)\)", re.DOTALL)


def restore_subproject_paths(
    command: str,
    output: str,
    entries: list[dict[str, typing.Any]],
) -> list[dict[str, typing.Any]]:
    owners: dict[tuple[str, typing.Any, typing.Any], set[str]] = {}
    for relative, body in _split_subproject_sections(output):
        for error in pyfltr.parsing.entry.parse_errors(command, body):
            owners.setdefault((error.file, error.line, error.message), set()).add(relative)
            legacy = _PYTEST_APPENDED_LOCATION_RE.fullmatch(error.message) if command == "pytest" and error.line == 0 else None
            if legacy is not None:
                owners.setdefault((legacy["file"], int(legacy["line"]), legacy["message"]), set()).add(relative)
    if not owners:
        return entries
    restored: list[dict[str, typing.Any]] = []
    for entry in entries:
        file = entry.get("file")
        messages = entry.get("messages")
        if not isinstance(file, str) or not isinstance(messages, list) or not _is_plain_relative(file):
            restored.append(entry)
            continue
        groups: dict[str | None, list[typing.Any]] = {}
        for message in messages:
            relatives = (
                owners.get((file, message.get("line"), message.get("msg")), set()) if isinstance(message, dict) else set()
            )
            owner = next(iter(relatives)) if len(relatives) == 1 else None
            groups.setdefault(owner, []).append(message)
        if list(groups) == [None]:
            restored.append(entry)
            continue
        for owner, owned_messages in groups.items():
            target = file if owner is None else posixpath.normpath(posixpath.join(owner, file))
            restored.append({**entry, "file": target, "messages": owned_messages})
    return restored


def _split_subproject_sections(output: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    relative: str | None = None
    lines: list[str] = []
    for line in [*output.splitlines(), "# external paths"]:
        match = _SECTION_HEADER_RE.match(line)
        if match is None:
            lines.append(line)
            continue
        if relative is not None:
            sections.append((relative, "\n".join(lines)))
        relative = match.group("relative")
        lines = []
    return sections


def _is_plain_relative(path: str) -> bool:
    pure = pathlib.PurePosixPath(path)
    return (
        bool(path)
        and not pure.is_absolute()
        and not pathlib.PureWindowsPath(path).anchor
        and ".." not in pure.parts
        and not path.startswith("<")
    )


def policy_from_config(config: pyfltr.config.model.Config) -> ArchivePolicy:
    return ArchivePolicy(
        max_runs=int(config.values.get("archive-max-runs", 100)),
        max_size_bytes=int(config.values.get("archive-max-size-mb", 1024)) * 1024 * 1024,
        max_age_days=int(config.values.get("archive-max-age-days", 30)),
    )
