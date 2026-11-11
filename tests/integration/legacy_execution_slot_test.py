"""旧版の実行枠保持者と現行版が同じロック保存先で排他を保つ。"""

import json
import pathlib
import subprocess
import sys
import time

import pytest

import pyfltr.command.execution_slot

# 協調API導入前の実体をFileLock・キャッシュ解決と共に再現する。CIはfetch-depth: 0で取得する。
_LEGACY_COMMIT = "60855c2a81737bac9fd717781b8b98c01d29f6f2"
_WORKER = """import json
import pathlib
import sys

role, package, directory = sys.argv[1:]
# packageに旧版の実体を渡した場合は旧版、"-"なら現行版を読み込む。
if package != "-":
    sys.path.insert(0, package)

import pyfltr.command.execution_slot as slot
import pyfltr.file_lock

root = pathlib.Path(directory)
with slot.hold(lock_dir=root / "locks", on_wait=lambda _: (root / (role + "-waiting")).touch()):
    (root / (role + "-entered")).touch()
    # oldとholdは標準入力が閉じられるまで枠を保持し続ける。
    if role in ("old", "hold"):
        sys.stdin.buffer.read()
    print(json.dumps({"slot": slot.__file__, "lock": pyfltr.file_lock.__file__, "role": role}), flush=True)
"""


def _prepare_legacy(tmp_path: pathlib.Path, repository: pathlib.Path) -> pathlib.Path:
    """旧版の実体を一時ディレクトリへ取り出し、パッケージのルートを返す。"""
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
    return legacy


@pytest.mark.parametrize("slots", ["1", "2"])
def test_legacy_holder_excludes_current_process_until_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, slots: str
) -> None:
    """旧版保持中の新版は、枠が1つなら待機して旧版解放後に取得し、枠が2つなら2つ目の枠で待たずに入る。

    旧版は1つ目の枠だけを扱うため、新版の2つ目の枠と合わせても同時実行は2件を超えない。
    """
    monkeypatch.setenv(pyfltr.command.execution_slot.SLOTS_ENV, slots)
    repository = pathlib.Path(__file__).resolve().parents[2]
    legacy = _prepare_legacy(tmp_path, repository)
    package = legacy / "pyfltr"
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
            if sys.platform == "linux":
                owner_path = tmp_path / "locks" / "execution.owner.json"
                record = json.loads(owner_path.read_text(encoding="utf-8"))
                record["create_time"] += 9
                owner_path.write_text(json.dumps(record), encoding="utf-8")
                owner = pyfltr.command.execution_slot.read_owner(tmp_path / "locks")
                assert owner is not None and owner.is_alive()
            with subprocess.Popen(
                [*argv, "new", "-", str(tmp_path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as current:
                try:
                    if slots == "1":
                        _wait_for(tmp_path / "new-waiting", current)
                        assert not (tmp_path / "new-entered").exists()
                    else:
                        current.wait(timeout=30)
                        assert not (tmp_path / "new-waiting").exists()
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


def test_legacy_and_current_holders_keep_two_concurrent_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """旧版と新版が1件ずつ保持している間、新版の3件目は待ち、新版の保持者の解放後に入る。

    旧版は1つ目の枠だけを扱うため、新版の2つ目の枠と合わせて同時実行は2件に収まる。
    """
    monkeypatch.delenv(pyfltr.command.execution_slot.SLOTS_ENV, raising=False)
    repository = pathlib.Path(__file__).resolve().parents[2]
    legacy = _prepare_legacy(tmp_path, repository)
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    argv = [sys.executable, "-I", str(worker)]
    with subprocess.Popen(
        [*argv, "old", str(legacy), str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as old:
        try:
            _wait_for(tmp_path / "old-entered", old)
            with subprocess.Popen(
                [*argv, "hold", "-", str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as holder:
                try:
                    _wait_for(tmp_path / "hold-entered", holder)
                    assert not (tmp_path / "hold-waiting").exists()
                    with subprocess.Popen(
                        [*argv, "new", "-", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    ) as third:
                        try:
                            _wait_for(tmp_path / "new-waiting", third)
                            assert not (tmp_path / "new-entered").exists()
                            # 新版の保持者が解放すると、3件目は空いた2つ目の枠へ入って終了する。
                            _, holder_error = holder.communicate(timeout=10)
                            third.wait(timeout=30)
                            assert (tmp_path / "new-entered").exists()
                            assert old.poll() is None
                        finally:
                            _, third_error = third.communicate(timeout=10)
                finally:
                    holder.communicate(timeout=10)
        finally:
            _, old_error = old.communicate(timeout=10)
    assert old.returncode == 0, old_error
    assert holder.returncode == 0, holder_error
    assert third.returncode == 0, third_error


def test_current_request_uses_second_slot_behind_legacy_waiter(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """旧版の要求が待機列の先頭で1つ目の枠を待つ間も、後から並んだ新版の要求は空いた2つ目の枠で待たずに入る。

    旧版の要求は1つ目の枠しか取得できないため、登録順を守ると2つ目の枠が使われないまま全員が待つ。
    """
    monkeypatch.delenv(pyfltr.command.execution_slot.SLOTS_ENV, raising=False)
    repository = pathlib.Path(__file__).resolve().parents[2]
    legacy = _prepare_legacy(tmp_path, repository)
    worker = tmp_path / "worker.py"
    worker.write_text(_WORKER, encoding="utf-8")
    argv = [sys.executable, "-I", str(worker)]
    with subprocess.Popen(
        [*argv, "old", str(legacy), str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as old:
        try:
            _wait_for(tmp_path / "old-entered", old)
            with subprocess.Popen(
                [*argv, "old2", str(legacy), str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
            ) as old2:
                try:
                    _wait_for(tmp_path / "old2-waiting", old2)
                    with subprocess.Popen(
                        [*argv, "new", "-", str(tmp_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
                    ) as current:
                        _, current_error = current.communicate(timeout=30)
                    assert current.returncode == 0, current_error
                    assert (tmp_path / "new-entered").exists()
                    assert not (tmp_path / "new-waiting").exists()
                    assert not (tmp_path / "old2-entered").exists()
                    # 旧版の保持者が解放すると、先頭の旧版の要求が1つ目の枠へ入る。
                    _, old_error = old.communicate(timeout=10)
                    _, old2_error = old2.communicate(timeout=30)
                finally:
                    old2.communicate(timeout=30)
        finally:
            old.communicate(timeout=10)
    assert old.returncode == 0, old_error
    assert old2.returncode == 0, old2_error
    assert (tmp_path / "old2-entered").exists()


def _wait_for(path: pathlib.Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        assert process.poll() is None, f"待機対象のプロセスが終了した: {path}"
        assert time.monotonic() < deadline, path
        time.sleep(0.01)
