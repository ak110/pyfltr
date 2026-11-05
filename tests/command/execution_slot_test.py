"""検証の実行枠（`pyfltr.command.execution_slot.hold`）の取得規則の試験。

別プロセスの保持者は、試験が所有するロック保存先で保持者スクリプトを起動して再現する。
"""

import collections.abc
import contextlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import psutil
import pytest

import pyfltr.command.execution_slot

_HOLDER_SCRIPT = """import os
import pathlib
import sys

import pyfltr.command.execution_slot

lock_dir = pathlib.Path(sys.argv[1])
with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, label="holder"):
    print("held", os.getpid(), flush=True)
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
def _holder(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> collections.abc.Generator[int, None, None]:
    """別プロセスで実行枠を保持し、保持を確認してから保持者のpidを渡す。区間を抜けると標準入力を閉じて解放させる。

    Windowsの仮想環境の`python.exe`は本体のPythonを子プロセスとして起動する中継役のため、
    `Popen.pid`は枠を保持するプロセスと一致しない。保持者が自ら報告したpidを使う。
    """
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
            status, pid = process.stdout.readline().split()
            assert status == "held"
            yield int(pid)
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
    with _holder(tmp_path, lock_dir) as holder_pid:
        owners: list[pyfltr.command.execution_slot.SlotOwner | None] = []
        with (
            pytest.raises(pyfltr.command.execution_slot.WaitAborted),
            pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: bool(owners), on_wait=owners.append),
        ):
            pytest.fail("保持者がいる間は区間へ入らない")
        assert len(owners) == 1
        assert owners[0] is not None
        assert owners[0].pid == holder_pid
        assert owners[0].label == "holder"
        current = pyfltr.command.execution_slot.read_owner(lock_dir)
        assert current is not None
        assert current.pid == holder_pid
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


def test_yield_preserves_waiting_order(lock_dir: pathlib.Path) -> None:
    """複数の待機者の登録順を保ち、譲った保持者は最後に再開する。"""
    events: list[str] = []
    threads = []

    def _other(label: str, waiting: threading.Event) -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting.set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait):
            events.append(label)

    assert not pyfltr.command.execution_slot.yield_to_waiters()
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        for label in ("first", "second"):
            waiting = threading.Event()
            thread = threading.Thread(target=_other, args=(label, waiting))
            threads.append(thread)
            thread.start()
            assert waiting.wait(30)
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
            assert not pyfltr.command.execution_slot.yield_requested()
            assert not pyfltr.command.execution_slot.yield_to_waiters()
            assert not events
        assert pyfltr.command.execution_slot.yield_requested()
        assert pyfltr.command.execution_slot.yield_to_waiters()
        events.append("resumed")
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    assert events == ["first", "second", "resumed"]
    assert not list(lock_dir.glob("execution.wait-*"))


_YIELDING_CHILD_SCRIPT = """import pathlib
import sys

import pyfltr.command.execution_slot

with pyfltr.command.execution_slot.hold(lock_dir=pathlib.Path(sys.argv[1]), should_abort=lambda: True):
    print(pyfltr.command.execution_slot.yield_requested(), pyfltr.command.execution_slot.yield_to_waiters(), flush=True)
"""

_YIELDING_HOLDER_SCRIPT = """import pathlib
import sys
import time

import pyfltr.command.execution_slot

lock_dir = pathlib.Path(sys.argv[1])
with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, label="holder"):
    print("held", flush=True)
    deadline = time.monotonic() + 60
    while not pyfltr.command.execution_slot.yield_requested():
        if time.monotonic() >= deadline:
            raise TimeoutError("待機者が現れません")
        time.sleep(0.02)
    pyfltr.command.execution_slot.yield_to_waiters()
    print("resumed", flush=True)
"""


def test_inherited_child_does_not_yield(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """保持者の子プロセスは待機者がいても譲らず、待機者は保持者の解放まで取得しない。"""
    script = tmp_path / "child.py"
    script.write_text(_YIELDING_CHILD_SCRIPT, encoding="utf-8")
    events: list[str] = []
    waiting = threading.Event()

    def _other() -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting.set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait):
            events.append("other-entered")

    thread = threading.Thread(target=_other)
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        thread.start()
        assert waiting.wait(30)
        completed = subprocess.run(
            [sys.executable, str(script), str(lock_dir)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert not events
        events.append("released")
    thread.join(timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.split() == ["False", "False"]
    assert events == ["released", "other-entered"]


def test_abort_while_reacquiring_releases_request(lock_dir: pathlib.Path) -> None:
    """取り直しの待機を中断すると枠を持たずに区間を抜け、待機要求を残さず、次の取得者は待たない。"""
    abort = threading.Event()
    waiting = threading.Event()
    finish = threading.Event()
    events: list[str] = []

    def _other() -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting.set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait):
            events.append("other-entered")
            abort.set()
            finish.wait(30)

    thread = threading.Thread(target=_other)
    with (
        pytest.raises(pyfltr.command.execution_slot.WaitAborted),
        pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=abort.is_set),
    ):
        thread.start()
        assert waiting.wait(30)
        pyfltr.command.execution_slot.yield_to_waiters()
        pytest.fail("中断した取り直しは区間へ戻らない")
    assert events == ["other-entered"]
    assert not list(lock_dir.glob("execution.wait-*"))
    finish.set()
    thread.join(timeout=30)
    assert not thread.is_alive()
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True):
        pass


def test_killed_while_reacquiring_is_reclaimed(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """取り直しを待つ保持者が強制終了しても、残った待機要求は回収され、次の取得者は待たない。

    待機者は保持者の祖先でない別プロセスにする。
    待機者がpytest自身のスレッドだと、取り直し時の保持者記録が保持者の祖先となって所有を継承し、取り直しの待機へ入らないためである。
    """
    script = tmp_path / "yielding_holder.py"
    script.write_text(_YIELDING_HOLDER_SCRIPT, encoding="utf-8")
    with subprocess.Popen([sys.executable, str(script), str(lock_dir)], stdout=subprocess.PIPE, text=True) as process:
        assert process.stdout is not None
        try:
            assert process.stdout.readline().strip() == "held"
            with _holder(tmp_path, lock_dir) as other_pid:
                # 保持者は解放より先に待機列へ再登録するため、待機者の保持中は保持者の要求だけが残る。
                assert len(list(lock_dir.glob("execution.wait-*"))) == 1
                current = pyfltr.command.execution_slot.read_owner(lock_dir)
                assert current is not None
                assert current.pid == other_pid
                # Windowsの仮想環境の`python.exe`は本体を子プロセスで起動するため、子孫も含めて止める。
                for child in psutil.Process(process.pid).children(recursive=True):
                    child.kill()
                process.kill()
                remaining, _ = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
    assert "resumed" not in remaining
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True):
        pass
    assert not list(lock_dir.glob("execution.wait-*"))


def _intervals(
    events: list[pyfltr.command.execution_slot.SlotWait],
) -> dict[tuple[str, int], list[pyfltr.command.execution_slot.SlotWait]]:
    """通知を`(kind, sequence)`ごとに開始・終了の順でまとめる。"""
    grouped: dict[tuple[str, int], list[pyfltr.command.execution_slot.SlotWait]] = {}
    for event in events:
        grouped.setdefault((event.kind, event.sequence), []).append(event)
    return grouped


def _assert_pair(pair: list[pyfltr.command.execution_slot.SlotWait], outcome: str) -> pyfltr.command.execution_slot.SlotWait:
    """開始（未完了）と終了（完了）の2通知が同じ区間を指すことを確かめ、終了の通知を返す。"""
    started, finished = pair
    assert (started.finished_at, started.elapsed, started.outcome) == (None, None, None)
    assert finished.started_at == started.started_at
    assert finished.finished_at is not None
    assert finished.elapsed is not None
    assert finished.elapsed >= 0
    assert finished.outcome == outcome
    return finished


def test_wait_interval_without_contention(lock_dir: pathlib.Path) -> None:
    """待機が無くても最初の取得を区間として通知し、入れ子の`hold`は通知しない。"""
    events: list[pyfltr.command.execution_slot.SlotWait] = []
    nested: list[pyfltr.command.execution_slot.SlotWait] = []
    with (
        pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait_interval=events.append),
        pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait_interval=nested.append),
    ):
        pass
    grouped = _intervals(events)
    assert list(grouped) == [("initial", 0)]
    finished = _assert_pair(grouped["initial", 0], "acquired")
    assert finished.elapsed is not None
    assert finished.elapsed < 1
    assert finished.to_dict()["kind"] == "initial"
    assert not nested


def test_wait_interval_measures_initial_wait(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """別プロセスが保持中は、解放までの待ちを最初の取得の区間として通知する。"""
    events: list[pyfltr.command.execution_slot.SlotWait] = []
    waiting = threading.Event()

    def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
        del owner
        waiting.set()

    def _other() -> None:
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait, on_wait_interval=events.append):
            pass

    thread = threading.Thread(target=_other)
    with _holder(tmp_path, lock_dir):
        thread.start()
        assert waiting.wait(30)
        # 待機の開始から解放までに一定の時間を置き、区間の秒数が待ちを含むことを確かめる。
        time.sleep(0.5)
    thread.join(timeout=30)
    finished = _assert_pair(_intervals(events)["initial", 0], "acquired")
    assert finished.elapsed is not None
    assert finished.elapsed >= 0.5


def test_wait_interval_counts_each_yield(lock_dir: pathlib.Path) -> None:
    """譲るたびに取り直しの区間を番号付きで通知し、待機者の順序は変えない。"""
    events: list[pyfltr.command.execution_slot.SlotWait] = []
    order: list[str] = []

    def _other(label: str, waiting: threading.Event) -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting.set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait):
            order.append(label)

    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait_interval=events.append):
        for label in ("first", "second"):
            waiting = threading.Event()
            thread = threading.Thread(target=_other, args=(label, waiting))
            thread.start()
            assert waiting.wait(30)
            assert pyfltr.command.execution_slot.yield_to_waiters()
            thread.join(30)
            order.append(f"resumed-{label}")
    grouped = _intervals(events)
    assert list(grouped) == [("initial", 0), ("reacquire", 1), ("reacquire", 2)]
    for pair in grouped.values():
        _assert_pair(pair, "acquired")
    assert order == ["first", "resumed-first", "second", "resumed-second"]


def test_wait_interval_reports_abort_and_interrupt(lock_dir: pathlib.Path) -> None:
    """待機を中断した区間は、中断の理由を持つ完了の通知で閉じる。"""
    aborted: list[pyfltr.command.execution_slot.SlotWait] = []
    interrupted: list[pyfltr.command.execution_slot.SlotWait] = []
    raised: list[str] = []

    def _raise_interrupt() -> bool:
        raise KeyboardInterrupt

    def _other() -> None:
        for should_abort, events in ((lambda: True, aborted), (_raise_interrupt, interrupted)):
            try:
                with pyfltr.command.execution_slot.hold(
                    lock_dir=lock_dir, should_abort=should_abort, on_wait_interval=events.append
                ):
                    raised.append("entered")
            except (pyfltr.command.execution_slot.WaitAborted, KeyboardInterrupt) as e:
                raised.append(type(e).__name__)

    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        thread = threading.Thread(target=_other)
        thread.start()
        thread.join(30)
        assert not thread.is_alive()
    assert raised == ["WaitAborted", "KeyboardInterrupt"]
    _assert_pair(_intervals(aborted)["initial", 0], "aborted")
    _assert_pair(_intervals(interrupted)["initial", 0], "interrupted")


_RECORDING_CHILD_SCRIPT = """import json
import pathlib
import sys

import pyfltr.command.execution_slot


def _record(wait):
    print(json.dumps(wait.to_dict()), flush=True)


with pyfltr.command.execution_slot.hold(lock_dir=pathlib.Path(sys.argv[1]), on_wait_interval=_record):
    print(json.dumps({"entered": True}), flush=True)
"""


def test_wait_interval_inherited_child(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """祖先が保持中の子は、継承を示す最初の取得の区間だけを通知し、祖先の待ちを重ねて数えない。"""
    script = tmp_path / "recording_child.py"
    script.write_text(_RECORDING_CHILD_SCRIPT, encoding="utf-8")
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        completed = subprocess.run(
            [sys.executable, str(script), str(lock_dir)], capture_output=True, text=True, timeout=60, check=False
        )
    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [record.get("outcome") for record in records] == [None, "inherited", None]
    assert {(record["kind"], record["sequence"]) for record in records[:2]} == {("initial", 0)}
    assert records[-1] == {"entered": True}


def _kill_if_running(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()


def test_wait_interval_external_caller(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """pyfltr以外の呼出元も同じ通知で、別プロセスの保持者を待った区間を記録できる。"""
    script = tmp_path / "recording_child.py"
    script.write_text(_RECORDING_CHILD_SCRIPT, encoding="utf-8")
    with contextlib.ExitStack() as stack:
        holder = stack.enter_context(contextlib.ExitStack())
        holder.enter_context(_holder(tmp_path, lock_dir))
        process = stack.enter_context(
            subprocess.Popen([sys.executable, str(script), str(lock_dir)], stdout=subprocess.PIPE, text=True)
        )
        stack.callback(_kill_if_running, process)
        assert process.stdout is not None
        started = json.loads(process.stdout.readline())
        assert started["finished_at"] is None
        time.sleep(0.5)
        # 呼出元は保持者の解放まで待つため、終了を待つ前に保持者の区間を抜ける。
        holder.close()
        remaining = process.stdout.read()
        assert process.wait(timeout=60) == 0
    finished, entered = [json.loads(line) for line in remaining.splitlines()]
    assert finished["outcome"] == "acquired"
    assert finished["started_at"] == started["started_at"]
    assert finished["elapsed"] >= 0.5
    assert entered == {"entered": True}


@pytest.mark.parametrize("reason", ["aborted", "interrupted"])
def test_wait_interval_reports_reacquire_abort(lock_dir: pathlib.Path, reason: str) -> None:
    """取り直しの待機を中断した区間も、中断の理由を持つ完了の通知で閉じ、枠を持たずに区間を抜ける。"""
    events: list[pyfltr.command.execution_slot.SlotWait] = []
    abort = threading.Event()
    waiting = threading.Event()
    finish = threading.Event()

    def _should_abort() -> bool:
        if not abort.is_set():
            return False
        if reason == "interrupted":
            raise KeyboardInterrupt
        return True

    def _other() -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting.set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait):
            abort.set()
            finish.wait(30)

    thread = threading.Thread(target=_other)
    expected = pyfltr.command.execution_slot.WaitAborted if reason == "aborted" else KeyboardInterrupt
    with (
        pytest.raises(expected),
        pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=_should_abort, on_wait_interval=events.append),
    ):
        thread.start()
        assert waiting.wait(30)
        pyfltr.command.execution_slot.yield_to_waiters()
        pytest.fail("中断した取り直しは区間へ戻らない")
    finish.set()
    thread.join(timeout=30)
    grouped = _intervals(events)
    assert list(grouped) == [("initial", 0), ("reacquire", 1)]
    _assert_pair(grouped["initial", 0], "acquired")
    _assert_pair(grouped["reacquire", 1], reason)
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True):
        pass


class _RecordFailure(Exception):
    """待機区間の記録に失敗したことを模す例外。"""


@pytest.mark.parametrize("failing", ["initial", "reacquire"])
def test_wait_interval_callback_failure_releases_slot(lock_dir: pathlib.Path, failing: str) -> None:
    """終了の通知が例外を送出しても取得済みの枠を解放し、例外を呼出元へ伝える。"""

    def _record(wait: pyfltr.command.execution_slot.SlotWait) -> None:
        if wait.kind == failing and wait.finished_at is not None:
            raise _RecordFailure

    waiting = threading.Event()
    entered: list[str] = []

    def _waiter() -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting.set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait=_on_wait):
            entered.append("waiter")

    with pytest.raises(_RecordFailure), pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait_interval=_record):
        if failing == "reacquire":
            thread = threading.Thread(target=_waiter)
            thread.start()
            assert waiting.wait(30)
            pyfltr.command.execution_slot.yield_to_waiters()
            thread.join(30)
        pytest.fail("記録に失敗した取得は区間へ入らない")
    assert pyfltr.command.execution_slot.read_owner(lock_dir) is None
    # 枠が解放されていれば、別スレッドは待たずに取得できる。
    acquired: list[bool] = []

    def _other() -> None:
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True):
            acquired.append(True)

    other = threading.Thread(target=_other)
    other.start()
    other.join(30)
    assert acquired == [True]
