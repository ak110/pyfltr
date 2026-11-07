"""外部所有下のstage_runnerと独立検証の譲渡・取消・回収。"""

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import pytest

import pyfltr.command.core_
import pyfltr.command.execution_slot as slot
import pyfltr.command.stage_runner
import pyfltr.config.config


def test_cooperative_nested_hold_yields(tmp_path: pathlib.Path) -> None:
    """協調所有者の同じスレッドの入れ子だけが所有者へ譲渡を委譲する。"""
    waiting = threading.Event()
    events = []

    def waiter() -> None:
        with slot.hold(lock_dir=tmp_path, on_wait=lambda _: waiting.set()):
            events.append("waiter")

    with slot.cooperative_hold(lock_dir=tmp_path):
        thread = threading.Thread(target=waiter)
        thread.start()
        try:
            assert waiting.wait(10)
            with slot.hold(lock_dir=tmp_path):
                assert slot.yield_requested()
                assert slot.yield_to_waiters()
                events.append("resumed")
        finally:
            thread.join(10)
    assert not thread.is_alive()
    assert events == ["waiter", "resumed"]


def test_legacy_hold_cannot_be_promoted(tmp_path: pathlib.Path) -> None:
    """内側だけの協調化で祖先の所有を解除しない。"""
    with slot.hold(lock_dir=tmp_path), pytest.raises(ValueError, match="最外側"), slot.cooperative_hold(lock_dir=tmp_path):
        pytest.fail("内側では協調所有者になれない")


@pytest.mark.parametrize("ticket", ["{}", "[]", "null", '{"coordinator":"../../x","participant":"a"}'])
def test_invalid_participant_ticket(tmp_path: pathlib.Path, ticket: str) -> None:
    """外部文字列から任意パスを参照せず、不正な参加券を入口で拒否する。"""
    with pytest.raises(ValueError), slot.hold(lock_dir=tmp_path, participant=ticket):
        pytest.fail("不正な参加券では参加できない")


@pytest.mark.parametrize(
    "outcome",
    [
        "normal",
        "parallel-normal",
        "crash",
        "child-abort",
        "child-abort-reacquire",
        "owner-abort",
        "owner-error",
        "cancel-waiter",
    ],
)
def test_external_owner_and_stage_runner(tmp_path: pathlib.Path, outcome: str) -> None:
    """兄弟の回収前は排他を保ち、全対象の区切りで先行待機者へ譲る。"""
    script = str(pathlib.Path(__file__).resolve())
    (tmp_path / "sample.py").touch()
    if outcome == "parallel-normal":
        (tmp_path / "parallel").touch()
    if outcome == "cancel-waiter":
        (tmp_path / "freeze-owner").touch()
    with (
        subprocess.Popen([sys.executable, script, "owner", str(tmp_path)], stderr=subprocess.PIPE) as owner,
    ):
        try:
            _wait_for(tmp_path / "first")
            if outcome == "parallel-normal":
                _wait_for(tmp_path / "parallel-second-active")
            _wait_for(tmp_path / "sibling-active")
            with subprocess.Popen([sys.executable, script, "waiter", str(tmp_path)], stderr=subprocess.PIPE) as waiter:
                try:
                    _wait_for(tmp_path / "waiting")
                    (tmp_path / "boundary").touch()
                    # 子のpaused到達を状態の公開効果（後続未開始）と合わせて確認する。
                    deadline = time.monotonic() + 10
                    controls = list((tmp_path / "locks").glob("execution.cooperative-*.json"))
                    assert len(controls) == 1
                    while True:
                        assert time.monotonic() < deadline
                        try:
                            state = json.loads(controls[0].read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            continue
                        if any(value["state"] == "paused" for value in state["participants"].values()):
                            break
                        assert time.monotonic() < deadline
                        time.sleep(0.01)
                    assert not (tmp_path / "waiter-ran").exists()
                    assert not (tmp_path / "second").exists()
                    if outcome in ("crash", "child-abort"):
                        (tmp_path / outcome).touch()
                    if outcome == "cancel-waiter":
                        (tmp_path / "cancel-waiter").touch()
                        assert waiter.wait(timeout=10) == 7
                        (tmp_path / "freeze-owner").unlink()
                    if outcome == "owner-error":
                        (tmp_path / "child-abort").touch()
                        (tmp_path / "owner-error").touch()
                    (tmp_path / "sibling-release").touch()
                    if outcome != "cancel-waiter":
                        _wait_for(tmp_path / "waiter-ran")
                        assert not (tmp_path / "second").exists()
                    if outcome == "child-abort-reacquire":
                        (tmp_path / "child-abort").touch()
                        # 再取得を許可する前に、子の取消が観測されるまで待つ。
                        _wait_for(tmp_path / "child-waits.json")
                    if outcome == "owner-abort":
                        (tmp_path / "owner-abort").touch()
                        assert owner.wait(timeout=10) == 7
                    (tmp_path / "waiter-release").touch()
                    if outcome != "cancel-waiter":
                        assert waiter.wait(timeout=10) == 0
                finally:
                    (tmp_path / "waiter-release").touch()
                    waiter.communicate(timeout=30)
        finally:
            for name in ("boundary", "sibling-release", "waiter-release"):
                (tmp_path / name).touch()
            _, errors = owner.communicate(timeout=30)
    assert owner.returncode == {"owner-abort": 7, "owner-error": 1}.get(outcome, 0), errors
    assert (tmp_path / "second").exists() is (outcome in ("normal", "parallel-normal", "crash", "cancel-waiter"))
    child_results = json.loads((tmp_path / "child-results.json").read_text(encoding="utf-8"))
    if outcome in ("child-abort", "child-abort-reacquire", "owner-abort", "owner-error"):
        skipped = next(result for result in child_results if result["command"] == "prettier")
        assert skipped["status"] == "skipped"
        assert skipped["returncode"] is None
        cause = "協調区間が閉じられました。" if outcome == "owner-abort" else "協調参加した子の処理が取り消されました。"
        assert skipped["output"] == cause + "所有者と子を回収してから再実行してください。"
    owner_waits = json.loads((tmp_path / "owner-waits.json").read_text(encoding="utf-8"))
    expected = [("initial", 0, None), ("initial", 0, "acquired")]
    if outcome not in ("owner-error", "cancel-waiter"):
        expected += [("reacquire", 1, None), ("reacquire", 1, "aborted" if outcome == "owner-abort" else "acquired")]
    assert [(value["kind"], value["sequence"], value["outcome"]) for value in owner_waits] == expected
    child_waits = json.loads((tmp_path / "child-waits.json").read_text(encoding="utf-8"))
    assert [(value["kind"], value["outcome"]) for value in child_waits] == [("initial", None), ("initial", "inherited")]
    assert slot.read_owner(tmp_path / "locks") is None
    assert not list((tmp_path / "locks").glob("execution.cooperative-*.json"))


def _wait_for(path: pathlib.Path) -> None:
    """試験が制御する区切りを上限付きで待つ。"""
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(0.01)


def _scenario_child(root: pathlib.Path, ticket: str) -> int:
    """stage_runnerのformatter間で安全に停止し、再取得後に残りを開始する。"""
    waits = []
    config = pyfltr.config.config.create_default_config()
    parallel = (root / "parallel").exists()
    commands = ["mypy", "ruff-check", "ty"] if parallel else ["ruff-format", "prettier"]
    config.values.update(dict.fromkeys(commands, True))
    config.values["jobs"] = 2
    started: list[str] = []
    guard = threading.Lock()

    def execute(command: str, _fix: bool) -> pyfltr.command.core_.CommandResult:
        with guard:
            index = len(started)
            started.append(command)
        if index < (2 if parallel else 1):
            (root / "first").touch()
            if index == 1:
                (root / "parallel-second-active").touch()
            _wait_for(root / "boundary")
        else:
            assert (root / "waiter-ran").exists() or (root / "cancel-waiter").exists()
            (root / "second").touch()
        return pyfltr.command.core_.CommandResult(
            command=command,
            command_type="formatter",
            commandline=[],
            returncode=0,
            files=1,
            output="",
            elapsed=0,
        )

    try:
        with slot.hold(
            lock_dir=root / "locks",
            participant=ticket,
            should_abort=(root / "child-abort").exists,
            on_wait_interval=lambda value: waits.append(value.to_dict()),
        ):
            results = pyfltr.command.stage_runner.run_stages(
                commands,
                pyfltr.command.core_.ExecutionBaseContext(
                    config=config, all_files=[root / "sample.py"], start_cwd=root, cache_store=None, cache_run_id=None
                ),
                execute,
            )
            (root / "child-results.json").write_text(
                json.dumps(
                    [
                        {
                            "command": result.command,
                            "status": result.status,
                            "returncode": result.returncode,
                            "output": result.output,
                        }
                        for result in results
                    ]
                ),
                encoding="utf-8",
            )
        # stage_runnerはWaitAbortedをskippedへ変換するため、残りが未実行でも正常に回収できる。
        return 0
    finally:
        (root / "child-waits.json").write_text(json.dumps(waits), encoding="utf-8")


def _scenario_sibling(root: pathlib.Path, ticket: str) -> int:
    """別の参加者が稼働中の区間を再現する。"""
    with slot.hold(lock_dir=root / "locks", participant=ticket):
        (root / "sibling-active").touch()
        _wait_for(root / "sibling-release")
        if (root / "crash").exists():
            os._exit(3)
    return 0


def _scenario_owner(root: pathlib.Path) -> int:
    """参加区間を子の回収後に閉じ、checkpointの合間に全子を管理する。"""
    waits = []
    children: list[tuple[subprocess.Popen, contextlib.ExitStack]] = []
    try:
        with (
            slot.cooperative_hold(
                lock_dir=root / "locks",
                should_abort=(root / "owner-abort").exists,
                on_wait_interval=lambda value: waits.append(value.to_dict()),
            ) as coordinator,
            contextlib.ExitStack() as processes,
        ):
            try:
                for mode in ("child", "sibling"):
                    # 参加区間は子の回収後に閉じるため、子ごとのExitStackで寿命を持たせる。
                    registration = contextlib.ExitStack()
                    ticket = registration.enter_context(coordinator.participant())
                    process = processes.enter_context(subprocess.Popen([sys.executable, __file__, mode, str(root), ticket]))
                    children.append((process, registration))
                deadline = time.monotonic() + 25
                while children:
                    for process, registration in children[:]:
                        if process.poll() is not None:
                            process.wait()
                            registration.close()
                            children.remove((process, registration))
                    if (root / "owner-error").exists():
                        raise RuntimeError("owner-error")
                    if not (root / "freeze-owner").exists():
                        coordinator.checkpoint()
                    if time.monotonic() > deadline:
                        raise TimeoutError("owner")
                    time.sleep(0.01)
            finally:
                # 取消・所有者例外でも、稼働中の処理を回収してから最外側の枠を解放する。
                for process, registration in children:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=10)
                    registration.close()
    except slot.WaitAborted:
        return 7
    finally:
        (root / "owner-waits.json").write_text(json.dumps(waits), encoding="utf-8")
    return 0


def _scenario_waiter(root: pathlib.Path) -> int:
    """所有者と祖先関係のないプロセスで同じ実行枠を待つ。"""
    try:
        with slot.hold(
            lock_dir=root / "locks",
            on_wait=lambda _: (root / "waiting").touch(),
            should_abort=(root / "cancel-waiter").exists,
        ):
            (root / "waiter-ran").touch()
            _wait_for(root / "waiter-release")
    except slot.WaitAborted:
        return 7
    return 0


def _scenario_main() -> int:
    """試験が指定した役割のプロセスを実行する。"""
    role, directory, *arguments = sys.argv[1:]
    root = pathlib.Path(directory)
    if role == "owner":
        return _scenario_owner(root)
    if role == "waiter":
        return _scenario_waiter(root)
    if role == "child":
        return _scenario_child(root, arguments[0])
    if role == "sibling":
        return _scenario_sibling(root, arguments[0])
    raise ValueError(role)


if __name__ == "__main__":
    sys.exit(_scenario_main())
