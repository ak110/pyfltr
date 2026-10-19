"""検証の実行枠（`pyfltr.command.execution_slot.hold`）の取得規則の試験。

別プロセスの保持者は、試験が所有するロック保存先で保持者スクリプトを起動して再現する。
"""

import collections.abc
import contextlib
import os
import pathlib
import subprocess
import sys
import threading

import pytest

import pyfltr.command.execution_slot

_HOLDER_SCRIPT = """import pathlib
import sys

import pyfltr.command.execution_slot

lock_dir = pathlib.Path(sys.argv[1])
with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, label="holder"):
    print("held", flush=True)
    sys.stdin.read()
"""

_CHILD_SCRIPT = """import pathlib
import sys

import pyfltr.command.execution_slot

# 祖先が保持中なら待たずに区間へ入る。継承できなければ待機に入り、中断判定でWaitAbortedになる。
with pyfltr.command.execution_slot.hold(lock_dir=pathlib.Path(sys.argv[1]), should_abort=lambda: True):
    print("entered", flush=True)
"""


@pytest.fixture(name="lock_dir")
def _lock_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "locks"


@contextlib.contextmanager
def _holder(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> collections.abc.Generator[subprocess.Popen[str], None, None]:
    """別プロセスで実行枠を保持し、保持を確認してから区間へ入る。区間を抜けると標準入力を閉じて解放させる。"""
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER_SCRIPT, encoding="utf-8")
    with subprocess.Popen(
        [sys.executable, str(script), str(lock_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        try:
            assert process.stdout.readline().strip() == "held"
            yield process
        finally:
            process.communicate(timeout=30)
    assert process.returncode == 0


def test_same_thread_reenters(lock_dir: pathlib.Path) -> None:
    """同じスレッドの入れ子は待たずに区間へ入る。"""
    with (
        pyfltr.command.execution_slot.hold(lock_dir=lock_dir),
        pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True),
    ):
        entered = True
    assert entered


def test_other_thread_waits_until_release(lock_dir: pathlib.Path) -> None:
    """同じプロセスの別スレッドは待機を通知し、保持者の解放後に区間へ入る。"""
    events: list[str] = []
    waiting = threading.Event()

    def _other() -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            assert owner is not None
            assert owner.pid == os.getpid()
            events.append("wait")
            waiting.set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait):
            events.append("other-entered")

    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        thread = threading.Thread(target=_other)
        thread.start()
        assert waiting.wait(timeout=30)
        events.append("released")
    thread.join(timeout=30)
    assert events == ["wait", "released", "other-entered"]


def test_child_process_inherits_owner(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """保持者の子プロセスは所有を継承し、自己待機せずに区間へ入る。"""
    script = tmp_path / "child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        # 環境変数を引き継がない子孫でも継承できることを、空に近い環境で確かめる。
        env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
        completed = subprocess.run(
            [sys.executable, str(script), str(lock_dir)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "entered"


def test_unrelated_process_waits_and_abort_does_not_acquire(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """祖先でない保持者がいると待機し、中断判定で取得せずに終わる。保持者の解放後は取得できる。"""
    with _holder(tmp_path, lock_dir) as holder:
        owners: list[pyfltr.command.execution_slot.SlotOwner | None] = []
        with (
            pytest.raises(pyfltr.command.execution_slot.WaitAborted),
            pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: bool(owners), on_wait=owners.append),
        ):
            pytest.fail("保持者がいる間は区間へ入らない")
        assert len(owners) == 1
        assert owners[0] is not None
        assert owners[0].pid == holder.pid
        assert owners[0].label == "holder"
        current = pyfltr.command.execution_slot.read_owner(lock_dir)
        assert current is not None
        assert current.pid == holder.pid
    assert pyfltr.command.execution_slot.read_owner(lock_dir) is None
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True):
        pass


def test_release_after_exception(lock_dir: pathlib.Path) -> None:
    """区間の内側で例外が起きても枠を解放し、次の取得は待たない。"""
    with pytest.raises(RuntimeError), pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        raise RuntimeError("boom")
    entered: list[bool] = []

    def _other() -> None:
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True):
            entered.append(True)

    thread = threading.Thread(target=_other)
    thread.start()
    thread.join(timeout=30)
    assert entered == [True]
