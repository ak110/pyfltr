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


@pytest.fixture(autouse=True)
def _single_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """既存の取得規則の試験は枠を1つにして、2件目が待つ前提で確かめる。枠が2つの試験は個別に上書きする。"""
    monkeypatch.setenv(pyfltr.command.execution_slot.SLOTS_ENV, "1")


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


@pytest.mark.skipif(sys.platform != "linux", reason="Linuxの壁時計補正と/procの同一性照合")
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("clock_shift", [-9, 9])
def test_clock_correction_preserves_liveness_and_inheritance(
    tmp_path: pathlib.Path, lock_dir: pathlib.Path, legacy: bool, clock_shift: int
) -> None:
    """時計補正後も保持者は生存と判定され、子は余分な枠を使わずに継承できる。"""
    script = tmp_path / "child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        path = lock_dir / "execution.owner.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        # 保存時と読取時のcreate_timeのずれを再現する。旧版は追加の2項目を持たない。
        record["create_time"] += clock_shift
        if legacy:
            record.pop("boot_id")
            record.pop("linux_start_time")
        path.write_text(json.dumps(record), encoding="utf-8")
        owner = pyfltr.command.execution_slot.read_owner(lock_dir)
        assert owner is not None and owner.is_alive()
        completed = subprocess.run(
            [sys.executable, str(script), str(lock_dir)],
            capture_output=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stdout.strip() == "entered"
        assert pyfltr.command.execution_slot.read_owner(lock_dir) == owner


@pytest.mark.skipif(sys.platform != "linux", reason="Linuxのboot_idとstarttimeの照合")
@pytest.mark.parametrize("changed", ["boot_id", "linux_start_time"])
def test_reused_pid_does_not_inherit_slot(tmp_path: pathlib.Path, lock_dir: pathlib.Path, changed: str) -> None:
    """PIDが一致しても、再起動前や別の生成時点の記録は生存・祖先と判定しない。"""
    script = tmp_path / "child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    with pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        path = lock_dir / "execution.owner.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if changed == "boot_id":
            record["boot_id"] = "previous-boot"
        else:
            record["linux_start_time"] += 1
        path.write_text(json.dumps(record), encoding="utf-8")
        owner = pyfltr.command.execution_slot.read_owner(lock_dir)
        assert owner is not None and not owner.is_alive()
        completed = subprocess.run(
            [sys.executable, str(script), str(lock_dir)],
            capture_output=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=30,
            check=False,
        )
        assert completed.returncode != 0
        assert "entered" not in completed.stdout


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


class _RecordFailure(Exception):
    """待機区間の記録に失敗したことを模す例外。"""


def test_wait_interval_callback_failure_releases_slot(lock_dir: pathlib.Path) -> None:
    """終了の通知が例外を送出しても取得済みの枠を解放し、例外を呼出元へ伝える。"""

    def _record(wait: pyfltr.command.execution_slot.SlotWait) -> None:
        if wait.finished_at is not None:
            raise _RecordFailure

    with pytest.raises(_RecordFailure), pyfltr.command.execution_slot.hold(lock_dir=lock_dir, on_wait_interval=_record):
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


@pytest.mark.parametrize(("raw", "expected"), [(None, 2), ("", 2), ("1", 1), ("2", 2)])
def test_slot_count_accepts_lowering_only(monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: int) -> None:
    """枠の数は既定で2つで、環境変数では1から既定値までを受け付ける。"""
    if raw is None:
        monkeypatch.delenv(pyfltr.command.execution_slot.SLOTS_ENV)
    else:
        monkeypatch.setenv(pyfltr.command.execution_slot.SLOTS_ENV, raw)
    assert pyfltr.command.execution_slot.slot_count() == expected


@pytest.mark.parametrize("raw", ["0", "3", "-1", "two"])
def test_slot_count_rejects_out_of_range(monkeypatch: pytest.MonkeyPatch, lock_dir: pathlib.Path, raw: str) -> None:
    """既定値を超える指定と整数でない指定は、指定できる値を示すエラーにする。"""
    monkeypatch.setenv(pyfltr.command.execution_slot.SLOTS_ENV, raw)
    with (
        pytest.raises(ValueError, match=pyfltr.command.execution_slot.SLOTS_ENV),
        pyfltr.command.execution_slot.hold(lock_dir=lock_dir),
    ):
        pytest.fail("不正な指定では区間へ入らない")


@pytest.mark.timeout(120)
def test_two_slots_let_second_process_enter_and_third_wait(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, lock_dir: pathlib.Path
) -> None:
    """枠が2つなら独立した2件目は待たずに入り、3件目は空きができるまで待つ。"""
    monkeypatch.delenv(pyfltr.command.execution_slot.SLOTS_ENV)
    with _holder(tmp_path, lock_dir) as first_pid:
        # 2件目の保持者は待たずに区間へ入り、保持を報告する（待つと報告が届かず試験が時間切れになる）。
        with _holder(tmp_path, lock_dir) as second_pid:
            owners = pyfltr.command.execution_slot.read_owners(lock_dir)
            assert sorted(owner.pid for owner in owners) == sorted([first_pid, second_pid])
            notified: list[pyfltr.command.execution_slot.SlotOwner | None] = []
            with (
                pytest.raises(pyfltr.command.execution_slot.WaitAborted),
                pyfltr.command.execution_slot.hold(
                    lock_dir=lock_dir, should_abort=lambda: bool(notified), on_wait=notified.append
                ),
            ):
                pytest.fail("2つの枠が埋まっている間は3件目は区間へ入らない")
            assert len(notified) == 1
        # 2件目の解放で空いた枠へ、待たずに入る。
        with pyfltr.command.execution_slot.hold(lock_dir=lock_dir, should_abort=lambda: True):
            pids = [owner.pid for owner in pyfltr.command.execution_slot.read_owners(lock_dir)]
            assert sorted(pids) == sorted([first_pid, os.getpid()])
    assert not pyfltr.command.execution_slot.read_owners(lock_dir)


@pytest.mark.timeout(120)
def test_child_inherits_owner_of_second_slot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, lock_dir: pathlib.Path
) -> None:
    """2つ目の枠の保持者の子プロセスも、所有を継承して待たずに区間へ入る。"""
    monkeypatch.delenv(pyfltr.command.execution_slot.SLOTS_ENV)
    script = tmp_path / "child.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    with _holder(tmp_path, lock_dir), pyfltr.command.execution_slot.hold(lock_dir=lock_dir):
        completed = subprocess.run(
            [sys.executable, str(script), str(lock_dir)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "entered"
