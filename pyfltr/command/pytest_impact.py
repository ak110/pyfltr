from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
import os
import pathlib
import subprocess
from collections.abc import Generator
from typing import Any

import pyfltr.file_lock
import pyfltr.paths

_PROBE = """
import hashlib, json, os, pathlib, sys
from importlib.metadata import distributions, version

if sys.version_info < (3, 12) or version("testscope") != "0.4.2":
    print(json.dumps({"reason": "Pythonまたはtestscopeの版が対象外"}))
else:
    from testscope.selection import MAP_VERSION, compute

    signature = hashlib.sha256(repr((
        sys.version.split()[0], sys.platform, sys.implementation.name,
        sorted((d.metadata["Name"], d.version) for d in distributions()),
        sys.argv[2:], os.environ.get("PYTEST_ADDOPTS"), os.environ.get("PYTEST_PLUGINS"),
        os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD"),
    )).encode()).hexdigest()
    result = {"signature": signature}
    if sys.argv[1] == "select":
        data = pathlib.Path(".testscope/map.json").read_bytes()
        stamp = json.loads(pathlib.Path(".testscope/pyfltr.json").read_bytes())
        m = json.loads(data)
        if stamp != [signature, hashlib.sha256(data).hexdigest()]:
            result["reason"] = "記録と実行環境が不一致"
        elif m["version"] != MAP_VERSION or m["python"] != sys.version.split()[0] or not m["tests"]:
            result["reason"] = "地図の形式またはPythonの版が不一致"
        else:
            sel = compute(os.getcwd(), m)
            if sel.full or sel.unobserved or any(
                pathlib.PurePosixPath(f).name in ("conftest.py", ".gitignore") for f in sel.changed
            ):
                result["reason"] = "設定または影響範囲を限定できないファイルの変更"
            else:
                result.update(tests=list(sel.tests), paths=list(sel.paths), known=list(m["tests"]))
    print(json.dumps(result, ensure_ascii=True))
"""


@dataclasses.dataclass(frozen=True)
class Probe:
    signature: str = ""
    reason: str = ""
    tests: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    known: tuple[str, ...] = ()


def probe(
    prefix: list[str],
    runner: str | None,
    cwd: pathlib.Path,
    env: dict[str, str],
    invocation: list[str],
    *,
    record: bool,
) -> Probe:
    python = _python_prefix(prefix, runner)
    if not python:
        return Probe(reason="pytestと同じPython環境を特定できない")
    try:
        completed = subprocess.run(
            [*python, "-c", _PROBE, "record" if record else "select", *invocation],
            cwd=cwd,
            env=env,
            capture_output=True,
            check=True,
            timeout=20,
        )
        data = json.loads(completed.stdout)
        if not isinstance(data, dict):
            raise ValueError
        for key in ("signature", "reason"):
            if key in data and not isinstance(data[key], str):
                raise ValueError
        lists = {}
        for key in ("tests", "paths", "known"):
            values = data.get(key, [])
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError
            lists[key] = tuple(values)
        if not data.get("signature") and not data.get("reason"):
            raise ValueError
        if not record and not lists["known"] and not data.get("reason"):
            raise ValueError
        return Probe(
            signature=data.get("signature", ""),
            reason=data.get("reason", ""),
            tests=lists["tests"],
            paths=lists["paths"],
            known=lists["known"],
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return Probe(reason="testscope未導入、履歴欠損、または選択処理の失敗")


def _python_prefix(prefix: list[str], runner: str | None) -> list[str]:
    if runner == "uv" and len(prefix) >= 2 and prefix[-1] == "pytest":
        return [*prefix[:-1], "python"]
    if runner == "direct" and len(prefix) == 1:
        script = pathlib.Path(prefix[0])
        if script.stem == "pytest" and (script.parent.parent / "pyvenv.cfg").is_file():
            python = script.with_name("python.exe" if os.name == "nt" else "python")
            if python.is_file():
                return [str(python)]
    return []


def select_files(
    scope: list[pathlib.Path],
    mandatory: list[pathlib.Path],
    result: Probe,
    *,
    start_cwd: pathlib.Path,
    cwd: pathlib.Path,
) -> list[pathlib.Path]:
    required = {pyfltr.paths.to_match_key(f, start_cwd=start_cwd) for f in mandatory}
    known = _node_files(result.known, cwd, start_cwd)
    affected = _node_files(result.tests, cwd, start_cwd)
    prefixes = [pyfltr.paths.to_match_key(cwd / f, start_cwd=start_cwd) for f in result.paths]
    selected = []
    for target in scope:
        key = pyfltr.paths.to_match_key(target, start_cwd=start_cwd)
        if (
            key in required
            or key in affected
            or key not in known
            or any(key == prefix or key.startswith(prefix.rstrip("/") + "/") for prefix in prefixes)
        ):
            selected.append(target)
    return selected


def _node_files(nodes: tuple[str, ...], cwd: pathlib.Path, start_cwd: pathlib.Path) -> set[str]:
    return {pyfltr.paths.to_match_key(cwd / node.split("::", 1)[0], start_cwd=start_cwd) for node in nodes}


@dataclasses.dataclass
class Recording:
    directory: pathlib.Path
    signature: str
    expected: set[str]
    start_cwd: pathlib.Path
    old_map: bytes | None
    complete: bool = False

    def accept(self) -> None:
        try:
            data = (self.directory / "map.json").read_bytes()
            if data == self.old_map:
                return
            mapping = json.loads(data)
            cwd = self.directory.parent
            if not isinstance(mapping, dict) or not isinstance(mapping.get("tests"), dict):
                return
            if self.expected != _node_files(tuple(mapping["tests"]), cwd, self.start_cwd):
                return
            if _loses_unchanged_tests(self.old_map, mapping):
                return
            (self.directory / "pyfltr.json").write_text(
                json.dumps([self.signature, hashlib.sha256(data).hexdigest()]), encoding="utf-8"
            )
        except (OSError, ValueError):
            return
        self.complete = True


def _loses_unchanged_tests(previous: bytes | None, mapping: dict[str, Any]) -> bool:
    if previous is None:
        return False
    try:
        older = json.loads(previous)
        missing = older["tests"].keys() - mapping["tests"].keys()
        old_snap, new_snap = older["snap"], mapping["snap"]
    except (ValueError, TypeError, AttributeError, KeyError):
        return False
    if not isinstance(old_snap, dict) or not isinstance(new_snap, dict):
        return False
    for node in missing:
        file = node.split("::", 1)[0]
        if file in old_snap and old_snap[file] == new_snap.get(file):
            return True
    return False


@contextlib.contextmanager
def recording(
    cwd: pathlib.Path, signature: str, scope: list[pathlib.Path], start_cwd: pathlib.Path
) -> Generator[Recording | None, None, None]:
    directory = cwd / ".testscope"
    names = ("map.json", "lastfailed.json", "pyfltr.json")
    lock_path = directory / "pyfltr.lock"
    if directory.is_symlink() or lock_path.is_symlink() or any((directory / name).is_symlink() for name in names):
        yield None
        return
    lock = pyfltr.file_lock.FileLock(lock_path)
    previous: dict[pathlib.Path, bytes | None] = {}
    transaction = None
    try:
        try:
            if lock.try_acquire() and not any(directory.glob("shard-*.json")):
                for name in names:
                    path = directory / name
                    previous[path] = path.read_bytes() if path.exists() else None
                expected = {pyfltr.paths.to_match_key(path, start_cwd=start_cwd) for path in scope}
                transaction = Recording(directory, signature, expected, start_cwd, previous[directory / "map.json"])
        except OSError:
            pass
        try:
            yield transaction
        finally:
            if transaction is not None and not transaction.complete:
                for path, content in previous.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(content)
    finally:
        lock.release()
