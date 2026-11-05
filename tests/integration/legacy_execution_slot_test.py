"""旧版の実行枠保持者と現行版が同じロック保存先で排他を保つ。"""

import json
import pathlib
import subprocess
import sys
import time

# 協調API導入前の実体をFileLock・キャッシュ解決と共に再現する。CIはfetch-depth: 0で取得する。
_LEGACY_COMMIT = "60855c2a81737bac9fd717781b8b98c01d29f6f2"
_WORKER = """import json
import pathlib
import sys

role, package, directory = sys.argv[1:]
if role == "old":
    sys.path.insert(0, package)

import pyfltr.command.execution_slot as slot
import pyfltr.file_lock

root = pathlib.Path(directory)
with slot.hold(lock_dir=root / "locks", on_wait=lambda _: (root / "waiting").touch()):
    (root / (role + "-entered")).touch()
    if role == "old":
        sys.stdin.buffer.read()
    print(json.dumps({"slot": slot.__file__, "lock": pyfltr.file_lock.__file__, "role": role}), flush=True)
"""


def test_legacy_holder_excludes_current_process_until_release(tmp_path: pathlib.Path) -> None:
    """旧版保持中の新版は待機し、旧版解放後に取得して両プロセスが正常終了する。"""
    repository = pathlib.Path(__file__).resolve().parents[2]
    legacy = tmp_path / "legacy"
    package = legacy / "pyfltr"
    (package / "command").mkdir(parents=True)
    (package / "__init__.py").touch()
    (package / "command" / "__init__.py").touch()
    for relative in ("file_lock.py", "user_cache.py", "command/execution_slot.py"):
        source = subprocess.run(
            ["git", "show", f"{_LEGACY_COMMIT}:pyfltr/{relative}"],
            cwd=repository,
            check=True,
            capture_output=True,
            timeout=10,
        )
        (package / relative).write_bytes(source.stdout)
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    argv = [sys.executable, "-I", str(worker)]
    with subprocess.Popen(
        [*argv, "old", str(legacy), str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as old:
        try:
            _wait_for(tmp_path / "old-entered", old)
            with subprocess.Popen(
                [*argv, "new", "-", str(tmp_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as current:
                try:
                    _wait_for(tmp_path / "waiting", current)
                    assert not (tmp_path / "new-entered").exists()
                    assert old.poll() is None
                finally:
                    old_output, old_error = old.communicate(timeout=10)
                    current_output, current_error = current.communicate(timeout=10)
        finally:
            old.communicate(timeout=10)
    assert old.returncode == 0, old_error
    assert current.returncode == 0, current_error
    assert (tmp_path / "new-entered").exists()
    old_modules = json.loads(old_output)
    current_modules = json.loads(current_output)
    assert pathlib.Path(old_modules["slot"]).resolve() == package / "command" / "execution_slot.py"
    assert pathlib.Path(old_modules["lock"]).resolve() == package / "file_lock.py"
    assert pathlib.Path(current_modules["slot"]).resolve() == repository / "pyfltr" / "command" / "execution_slot.py"
    assert pathlib.Path(current_modules["lock"]).resolve() == repository / "pyfltr" / "file_lock.py"


def _wait_for(path: pathlib.Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, f"待機対象のプロセスが終了した: {path}"
        assert time.monotonic() < deadline, path
        time.sleep(0.01)
