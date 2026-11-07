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
import typing

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
import typing

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


def _run_allocation_scenario(lock_dir: pathlib.Path) -> dict[str, typing.Any]:
    """長い保持者3件が短い区切りで譲り合う間に、短い処理1件を並べて待ちと進行を測る。

    長い処理は0.2秒の手順を12回、短い処理は0.02秒の手順を4回行い、各手順の後に`yield_to_waiters`を呼ぶ。
    保持区間の重なりを`active`で検出し、全処理の手順の完了と待機要求の回収を返す。
    """
    active: list[int] = [0]
    overlap: list[bool] = []
    lock = threading.Lock()
    steps: dict[str, int] = {}
    short: dict[str, float] = {}
    short_waits: list[pyfltr.command.execution_slot.SlotWait] = []
    long_finished: list[float] = []
    origin = time.monotonic()

    def _work(label: str, count: int, duration: float) -> None:
        for _ in range(count):
            with lock:
                active[0] += 1
                overlap.append(active[0] > 1)
            time.sleep(duration)
            with lock:
                active[0] -= 1
                steps[label] = steps.get(label, 0) + 1
            pyfltr.command.execution_slot.yield_to_waiters()

    def _long(label: str) -> None:
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, label=label):
            _work(label, 12, 0.2)
        long_finished.append(time.monotonic() - origin)

    def _short() -> None:
        started = time.monotonic()
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, label="short", on_wait_interval=short_waits.append):
            short["acquired"] = time.monotonic() - started
            _work("short", 4, 0.02)
        short["finished"] = time.monotonic() - started

    longs = [threading.Thread(target=_long, args=(f"long-{index}",)) for index in range(3)]
    for thread in longs:
        thread.start()
        time.sleep(0.1)
    # 長い処理が譲り合い、2件の取り直し要求が並んだ状態へ到達してから短い処理を始める。
    _wait_until(lambda: len(_reacquire_requests(lock_dir)) >= 2, "長い処理の取り直し要求が2件並ぶ")
    short_thread = threading.Thread(target=_short)
    short_thread.start()
    for thread in [*longs, short_thread]:
        thread.join(60)
        assert not thread.is_alive()
    completed = [wait for wait in short_waits if wait.finished_at is not None]
    return {
        "short_finished": short["finished"],
        "short_intervals": len(completed),
        "long_finished_max": max(long_finished),
        "steps": steps,
        "overlap": any(overlap),
        "leftover": sorted(path.name for path in lock_dir.glob("execution.wait-*")),
    }


def test_new_request_precedes_reacquire_and_finishes_in_one_segment(
    lock_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """短い新規要求は長い検証の取り直し要求の前に並び、最初の区間で譲らずに終わる。

    比較対象の旧方式（登録順だけで並べ、最初の区間も毎回譲る）は2つの定数を0にして再現する。
    """
    monkeypatch.setattr(pyfltr.command.execution_slot, "_REACQUIRE_BYPASS_SECONDS", 0.0)
    monkeypatch.setattr(pyfltr.command.execution_slot, "_FIRST_SEGMENT_HOLD_SECONDS", 0.0)
    before = _run_allocation_scenario(lock_dir / "fifo")
    monkeypatch.setattr(pyfltr.command.execution_slot, "_REACQUIRE_BYPASS_SECONDS", 30.0)
    monkeypatch.setattr(pyfltr.command.execution_slot, "_FIRST_SEGMENT_HOLD_SECONDS", 30.0)
    after = _run_allocation_scenario(lock_dir / "priority")
    print(f"allocation-scenario before={before} after={after}")
    for result in (before, after):
        assert result["steps"] == {"long-0": 12, "long-1": 12, "long-2": 12, "short": 4}
        assert not result["overlap"]
        assert result["leftover"] == []
    # 旧方式の短い処理は各手順の後に長い処理の区間を間に置いて待ち直す。
    assert before["short_intervals"] > 1
    assert after["short_intervals"] == 1
    assert after["short_finished"] < before["short_finished"] / 2
    # 長い処理の完了時刻（long_finished_max）は並列実行の負荷で変動するため、判定せず出力して記録する。


def _wait_until(predicate: collections.abc.Callable[[], bool], description: str, timeout: float = 30.0) -> None:
    """条件が成立するまで待つ。固定の待ち時間ではなく状態への到達で次の手順へ進めるため。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(description)
        time.sleep(0.01)


def _reacquire_requests(lock_dir: pathlib.Path) -> list[str]:
    return [path.name for path in lock_dir.glob("execution.wait-*") if path.name.split("-")[2].startswith("R")]


def _run_aging_order(lock_dir: pathlib.Path, *, bypass_seconds: float, age_reacquire: bool) -> list[str]:
    """長い保持者の取り直し要求の老化前後に新規要求を並べ、取得の順番を返す。

    1. 長い処理Lが保持中に新規要求S1が待ち、Lが譲って取り直し要求Rを登録し、S1が取得する
    2. S1の保持中に新規要求S2を並べる（Rは若い）
    3. `age_reacquire`ならRの登録から上限を超えるまで待ち、新規要求S3を並べる
    4. S1の解放後の取得順を記録する
    """
    lock_dir.mkdir(parents=True)
    order: list[str] = []
    release_s1 = threading.Event()
    waiting: dict[str, threading.Event] = {name: threading.Event() for name in ("S1", "S2", "S3")}

    def _short(name: str, *, hold_until: threading.Event | None = None) -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting[name].set()

        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, label=name, on_wait=_on_wait):
            order.append(name)
            if hold_until is not None:
                assert hold_until.wait(30)

    def _long() -> None:
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, label="L"):
            order.append("L")
            assert waiting["S1"].wait(30)
            assert pyfltr.command.execution_slot.yield_to_waiters()
            order.append("L-reacquired")

    threads = [threading.Thread(target=_long)]
    threads[0].start()
    _wait_until(lambda: order == ["L"], "Lが取得する")
    threads.append(threading.Thread(target=_short, args=("S1",), kwargs={"hold_until": release_s1}))
    threads[-1].start()
    _wait_until(lambda: order == ["L", "S1"] and len(_reacquire_requests(lock_dir)) == 1, "S1が取得しLの取り直しが並ぶ")
    threads.append(threading.Thread(target=_short, args=("S2",)))
    threads[-1].start()
    assert waiting["S2"].wait(30)
    if age_reacquire:
        registered = int(_reacquire_requests(lock_dir)[0].split("-")[2][1:]) / 1000
        _wait_until(lambda: time.time() - registered > bypass_seconds + 0.2, "取り直し要求が老化する")
    threads.append(threading.Thread(target=_short, args=("S3",)))
    threads[-1].start()
    assert waiting["S3"].wait(30)
    release_s1.set()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    assert not list(lock_dir.glob("execution.wait-*"))
    return order


def test_aged_reacquire_is_not_bypassed(lock_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """若い取り直し要求の前へは新規要求が入り、上限を過ぎた取り直し要求の前へは入らない。

    老化を無効にした場合（上限を十分に長くした場合）は、新規要求が続く限り長い処理の取り直しが後回しになる。
    """
    monkeypatch.setattr(pyfltr.command.execution_slot, "_REACQUIRE_BYPASS_SECONDS", 1.0)
    aged = _run_aging_order(lock_dir / "aged", bypass_seconds=1.0, age_reacquire=True)
    young = _run_aging_order(lock_dir / "young", bypass_seconds=1.0, age_reacquire=False)
    monkeypatch.setattr(pyfltr.command.execution_slot, "_REACQUIRE_BYPASS_SECONDS", 3600.0)
    no_aging = _run_aging_order(lock_dir / "no-aging", bypass_seconds=1.0, age_reacquire=True)
    print(f"aging-order aged={aged} young={young} no_aging={no_aging}")
    # 老化後に並んだS3はLの取り直しの後になる。
    assert aged == ["L", "S1", "S2", "L-reacquired", "S3"]
    # 若い取り直し要求の前へはS2・S3とも入る。
    assert young == ["L", "S1", "S2", "S3", "L-reacquired"]
    # 老化が働かなければ、上限を待ってもS3がLの取り直しより先になる。
    assert no_aging == ["L", "S1", "S2", "S3", "L-reacquired"]


def test_allocate_sequence_keeps_legacy_requests_in_order(lock_dir: pathlib.Path) -> None:
    """旧版の要求と印の無い要求の前へは新規要求を置かず、間に置ける番号が無い場合は末尾へ並べる。"""
    del lock_dir
    now_ms = int(time.time() * 1000)
    young = f"R{now_ms:013d}"
    allocate = pyfltr.command.execution_slot._allocate_sequence  # pylint: disable=protected-access

    def _paths(*names: str) -> list[pathlib.Path]:
        return [pathlib.Path(name) for name in names]

    step = pyfltr.command.execution_slot._SEQUENCE_STEP  # pylint: disable=protected-access
    # 旧版の要求（乱数部だけ）の後へ、若い取り直し要求の前へ置く。
    waiters = _paths(f"execution.wait-{step:020d}-abcd1234", f"execution.wait-{2 * step:020d}-{young}-x")
    sequence = allocate(waiters, reacquire=False, now_ms=now_ms)
    assert step < sequence < 2 * step
    # 取り直し要求は末尾へ並べる。
    assert allocate(waiters, reacquire=True, now_ms=now_ms) == 3 * step
    # 旧版が末尾へ1を足した連番で間に置ける番号が無い場合は末尾へ並べる。
    crowded = _paths(f"execution.wait-{5:020d}-abcd1234", f"execution.wait-{6:020d}-{young}-x")
    assert allocate(crowded, reacquire=False, now_ms=now_ms) == 6 + step
    # 経過上限を超えた取り直し要求の前へは置かない。
    aged = f"R{now_ms - 10**7:013d}"
    old = _paths(f"execution.wait-{step:020d}-{aged}-x")
    assert allocate(old, reacquire=False, now_ms=now_ms) == 2 * step


_JOIN_SCRIPT = """import os
import sys

import pyfltr.command.execution_slot as slot

lock_dir, ticket, mode = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with slot.hold(lock_dir=__import__("pathlib").Path(lock_dir), participant=ticket):
        if mode == "crash":
            # 参加の終了処理をせずに異常終了し、参加中の登録を残す。
            os._exit(3)
except slot.WaitAborted:
    sys.exit(9)
"""


def test_receive_ticket_removes_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """受け取った参加券は環境変数から除かれ、以後に起動する子孫へ継承されない。"""
    monkeypatch.setenv(pyfltr.command.execution_slot.TICKET_ENV, "ticket")
    assert pyfltr.command.execution_slot.receive_ticket() == "ticket"
    assert pyfltr.command.execution_slot.TICKET_ENV not in os.environ
    assert pyfltr.command.execution_slot.receive_ticket() is None


def test_tool_environ_sets_bound_ticket_and_strips_inherited() -> None:
    """ツールへは割り当てた参加券だけを渡し、割り当てが無ければ継承した参加券を除く。"""
    name = pyfltr.command.execution_slot.TICKET_ENV
    env = {"PATH": "/bin", name: "inherited"}
    assert pyfltr.command.execution_slot.tool_environ(env) == {"PATH": "/bin"}
    with pyfltr.command.execution_slot.bind_tool_ticket("tool"):
        assert pyfltr.command.execution_slot.tool_environ(env) == {"PATH": "/bin", name: "tool"}
    assert pyfltr.command.execution_slot.tool_environ({"PATH": "/bin"}) == {"PATH": "/bin"}


def test_coordinated_hold_without_coordinator_when_nested(lock_dir: pathlib.Path) -> None:
    """同じスレッドの入れ子では協調所有者にならず、参加券を発行しない。"""
    with pyfltr.command.execution_slot.coordinated_hold(lock_dir=lock_dir) as outer:
        assert outer is not None
        with pyfltr.command.execution_slot.coordinated_hold(lock_dir=lock_dir) as inner:
            assert inner is None


@pytest.mark.parametrize("first", ["finish", "crash"])
def test_ticket_is_reusable_after_participant_exits(tmp_path: pathlib.Path, lock_dir: pathlib.Path, first: str) -> None:
    """参加を終えた参加券と、参加中に異常終了したプロセスの参加券は、次に起動したプロセスが使える。

    同じツールが対象ごとに子プロセスを順に起動しても、各プロセスが同じ参加券で参加できることを確かめる。
    """
    script = tmp_path / "join.py"
    script.write_text(_JOIN_SCRIPT, encoding="utf-8")
    with (
        pyfltr.command.execution_slot.cooperative_hold(lock_dir=lock_dir) as coordinator,
        coordinator.participant() as ticket,
    ):
        returncodes = [
            subprocess.run([sys.executable, str(script), str(lock_dir), ticket, mode], check=False).returncode
            for mode in (first, "finish")
        ]
    assert returncodes == [3 if first == "crash" else 0, 0]


_BUSY_TICKET_SCRIPT = """import pathlib
import sys

import pyfltr.command.execution_slot as slot

lock_dir, ticket = pathlib.Path(sys.argv[1]), sys.argv[2]
with slot.coordinated_hold(lock_dir=lock_dir, participant=ticket) as coordinator:
    print("coordinator" if coordinator is not None else "inherited", flush=True)
"""


def test_busy_ticket_falls_back_to_inherited_section(tmp_path: pathlib.Path, lock_dir: pathlib.Path) -> None:
    """参加中の参加券を継承した子孫は、参加せずに祖先の所有を継承した区間で処理を続ける。

    参加券を受け取らないツール（pytestなど）の子孫は環境変数の参加券を継承する。
    参加を拒否された子孫が処理全体を中断すると、ツールの結果が全てスキップされて成功扱いになるため、継承した区間で続けることを確かめる。
    """
    join_script = tmp_path / "join.py"
    join_script.write_text(_JOIN_WAIT_SCRIPT, encoding="utf-8")
    busy_script = tmp_path / "busy.py"
    busy_script.write_text(_BUSY_TICKET_SCRIPT, encoding="utf-8")
    with (
        pyfltr.command.execution_slot.cooperative_hold(lock_dir=lock_dir) as coordinator,
        coordinator.participant() as ticket,
        subprocess.Popen([sys.executable, str(join_script), str(lock_dir), ticket, str(tmp_path)]) as joined,
    ):
        try:
            _wait_for_file(tmp_path / "joined")
            busy = subprocess.run(
                [sys.executable, str(busy_script), str(lock_dir), ticket],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
            )
        finally:
            (tmp_path / "release").touch()
            joined.wait(timeout=30)
    assert (busy.returncode, busy.stdout.strip()) == (0, "inherited"), busy.stderr
    assert joined.returncode == 0


_JOIN_WAIT_SCRIPT = """import pathlib
import sys
import time

import pyfltr.command.execution_slot as slot

lock_dir, ticket, root = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
with slot.hold(lock_dir=lock_dir, participant=ticket):
    (root / "joined").touch()
    deadline = time.monotonic() + 30
    while not (root / "release").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    slot.yield_to_waiters()
"""


def _wait_for_file(path: pathlib.Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        assert time.monotonic() < deadline, path
        time.sleep(0.01)
