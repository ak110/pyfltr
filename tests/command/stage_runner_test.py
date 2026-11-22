import concurrent.futures
import dataclasses
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import threading
import time

import psutil
import pytest

import pyfltr.command.core_
import pyfltr.command.execution_slot
import pyfltr.command.stage_runner
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.diagnostics
import pyfltr.warnings_
from tests.conftest import StageExecutorControl, make_command_result, make_execution_context


def test_stages_hold_slot_until_all_tools_finish(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pyfltr.command.execution_slot.SLOTS_ENV, "1")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyfltr.custom-commands.first]\ntype="formatter"\npath="first"\ntargets="*.txt"\n'
        '[tool.pyfltr.custom-commands.second]\ntype="formatter"\npath="second"\ntargets="*.txt"\n',
        encoding="utf-8",
    )
    target = tmp_path / "input.txt"
    target.write_text("x", encoding="utf-8")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    waiting = threading.Event()
    events: list[str] = []

    def _other() -> None:
        def _on_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
            del owner
            waiting.set()

        with pyfltr.command.execution_slot.hold(on_wait=_on_wait):
            events.append("other")

    thread = threading.Thread(target=_other)

    def _execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        del fix_stage
        events.append(command)
        if command == "first":
            thread.start()
            assert waiting.wait(30)
        return make_command_result(command, returncode=0)

    try:
        results = pyfltr.command.stage_runner.run_stages(
            ["first", "second"],
            make_execution_context(config, [target], start_cwd=tmp_path).base,
            _execute,
        )
    finally:
        if thread.ident is not None:
            thread.join(30)
    assert not thread.is_alive()
    assert events == ["first", "second", "other"]
    assert [result.command for result in results] == ["first", "second"]


def test_make_skipped_result_returns_command_result():
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("mypy", config)
    assert isinstance(result, pyfltr.command.core_.CommandResult)


def test_make_skipped_result_status_is_skipped():
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("mypy", config)
    assert result.status == "skipped"
    assert result.returncode is None


def test_make_skipped_result_preserves_command_name():
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("ruff-check", config)
    assert result.command == "ruff-check"


def test_make_skipped_result_is_not_failed():
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("mypy", config)
    assert result.failed is False


def _make_pending_future() -> concurrent.futures.Future:
    f: concurrent.futures.Future = concurrent.futures.Future()
    return f


def _make_done_future() -> concurrent.futures.Future:
    f: concurrent.futures.Future = concurrent.futures.Future()
    f.set_result(None)
    return f


def test_cancel_pending_futures_cancels_pending_only():
    aborted: set[str] = set()

    done_future = _make_done_future()
    pending_future = _make_pending_future()

    future_to_command = {
        done_future: "done-cmd",
        pending_future: "pending-cmd",
    }

    pyfltr.command.stage_runner.cancel_pending_futures(future_to_command, aborted)

    assert "done-cmd" not in aborted
    assert "pending-cmd" in aborted


def test_cancel_pending_futures_adds_cancelled_commands():
    aborted: set[str] = set()

    pending1 = _make_pending_future()
    pending2 = _make_pending_future()

    future_to_command = {
        pending1: "cmd-a",
        pending2: "cmd-b",
    }

    pyfltr.command.stage_runner.cancel_pending_futures(future_to_command, aborted)

    assert "cmd-a" in aborted
    assert "cmd-b" in aborted


def test_cancel_pending_futures_does_not_add_failed_cancel():
    aborted: set[str] = set()

    started = threading.Event()
    can_finish = threading.Event()

    def _blocking_task():
        started.set()
        can_finish.wait()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        running_future = executor.submit(_blocking_task)
        started.wait()

        future_to_command = {running_future: "running-cmd"}
        pyfltr.command.stage_runner.cancel_pending_futures(future_to_command, aborted)

        can_finish.set()

    assert "running-cmd" not in aborted


def _run_fail_fast_stage(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, run_on_submit: set[str]
) -> tuple[dict[str, str], list[str]]:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyfltr]
jobs = 2

[tool.pyfltr.custom-commands.failing]
type = "linter"
path = "failing"
targets = ["*.txt"]

[tool.pyfltr.custom-commands.follow-up]
type = "linter"
path = "follow-up"
targets = ["*.txt"]
""".lstrip(),
        encoding="utf-8",
    )
    target = tmp_path / "input.txt"
    target.write_text("x\n", encoding="utf-8")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)
    control = StageExecutorControl(run_on_submit=run_on_submit)
    control.install(monkeypatch)

    def _execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        del fix_stage
        return make_command_result(command, returncode=1 if command == "failing" else 0)

    results = pyfltr.command.stage_runner.run_stages(
        ["failing", "follow-up"],
        make_execution_context(config, [target], start_cwd=tmp_path).base,
        _execute,
        fail_fast=True,
    )
    return {result.command: result.status for result in results}, control.executed


def test_run_stages_fail_fast_skips_pending_follow_up(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    statuses, executed = _run_fail_fast_stage(tmp_path, monkeypatch, run_on_submit={"failing"})

    assert statuses == {"failing": "failed", "follow-up": "skipped"}
    assert executed == ["failing"]


def test_run_stages_fail_fast_keeps_completed_follow_up(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    statuses, executed = _run_fail_fast_stage(tmp_path, monkeypatch, run_on_submit={"failing", "follow-up"})

    assert statuses == {"failing": "failed", "follow-up": "succeeded"}
    assert sorted(executed) == ["failing", "follow-up"]


def _run_fix_failure_stages(
    tmp_path: pathlib.Path,
    *,
    fail_fast: bool,
    normal_returncode: int | None,
) -> tuple[list[pyfltr.command.core_.CommandResult], list[str], list[str]]:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyfltr]
jobs = 1

[tool.pyfltr.custom-commands.fixable]
type = "linter"
path = "fixable"
targets = ["*.txt"]
fix-args = ["--fix"]

[tool.pyfltr.custom-commands.follow-up]
type = "linter"
path = "follow-up"
targets = ["*.txt"]
""".lstrip(),
        encoding="utf-8",
    )
    target = tmp_path / "input.txt"
    target.write_text("x\n", encoding="utf-8")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)

    def _execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        if command == "fixable" and fix_stage:
            return make_command_result(command, returncode=1, output="fix failed")
        if command == "fixable":
            return make_command_result(command, returncode=normal_returncode, output="lint ok")
        return make_command_result(command, returncode=0)

    notified: list[str] = []
    archived: list[str] = []
    results = pyfltr.command.stage_runner.run_stages(
        ["fixable", "follow-up"],
        make_execution_context(config, [target], start_cwd=tmp_path).base,
        _execute,
        include_fix_stage=True,
        fail_fast=fail_fast,
        callbacks=pyfltr.command.stage_runner.StageCallbacks(
            archive=lambda result: archived.append(f"{result.command}:{result.status}"),
            on_result=lambda result: notified.append(f"{result.command}:{result.status}"),
        ),
    )
    return results, notified, archived


def test_run_stages_keeps_fix_failure_when_normal_stage_succeeds(tmp_path: pathlib.Path) -> None:
    results, notified, archived = _run_fix_failure_stages(tmp_path, fail_fast=False, normal_returncode=0)

    fixable = [result for result in results if result.command == "fixable"]
    assert len(fixable) == 1
    assert fixable[0].status == "failed"
    assert fixable[0].output == "fix failed\nlint ok"
    assert notified.count("fixable:failed") == 1
    assert "fixable:succeeded" not in notified
    assert archived.count("fixable:failed") == 1
    assert "fixable:succeeded" not in archived


def test_run_stages_fail_fast_records_fix_failure_once(tmp_path: pathlib.Path) -> None:
    results, notified, archived = _run_fix_failure_stages(tmp_path, fail_fast=True, normal_returncode=0)

    statuses = {result.command: result.status for result in results}
    assert statuses == {"fixable": "failed", "follow-up": "skipped"}
    assert [result.command for result in results].count("fixable") == 1
    assert notified.count("fixable:failed") == 1
    assert archived.count("fixable:failed") == 1


def test_merge_fix_failure_keeps_fix_diagnostics_when_normal_stage_has_none() -> None:
    config = pyfltr.config.config.create_default_config()
    diagnostic = pyfltr.diagnostics.ErrorLocation(file="x.py", line=1, col=None, command="ruff-check", message="F821")
    fix_result = make_command_result("ruff-check", returncode=1, errors=[diagnostic])

    skipped = pyfltr.command.stage_runner.merge_fix_failure(
        fix_result, pyfltr.command.stage_runner.make_skipped_result("ruff-check", config)
    )
    succeeded = pyfltr.command.stage_runner.merge_fix_failure(fix_result, make_command_result("ruff-check", returncode=0))

    assert skipped.status == "failed"
    assert skipped.errors == [diagnostic]
    assert succeeded.status == "failed"
    assert succeeded.errors == [diagnostic]


def test_merge_fix_failure_does_not_duplicate_normal_stage_diagnostics() -> None:
    diagnostic = pyfltr.diagnostics.ErrorLocation(file="x.py", line=1, col=None, command="ruff-check", message="F821")
    fix_result = make_command_result("ruff-check", returncode=1, errors=[diagnostic])
    normal_result = make_command_result("ruff-check", returncode=1, errors=[diagnostic])

    merged = pyfltr.command.stage_runner.merge_fix_failure(fix_result, normal_result)

    assert merged.status == "failed"
    assert merged.errors == [diagnostic]


def test_run_stages_sets_body_interval_only_for_executed_results(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "".join(
            f'[tool.pyfltr.custom-commands.{name}]\ntype="formatter"\npath="{name}"\ntargets="*.txt"\n'
            for name in ("ran", "skipped", "unresolved", "cached")
        ),
        encoding="utf-8",
    )
    target = tmp_path / "input.txt"
    target.write_text("x", encoding="utf-8")
    config = pyfltr.config.config.load_config(config_dir=tmp_path)

    def _execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        del fix_stage
        if command == "ran":
            return make_command_result(command, returncode=0)
        if command == "skipped":
            return make_command_result(command, returncode=None, files=0)
        if command == "unresolved":
            return make_command_result(command, returncode=1, resolution_failed=True)
        return make_command_result(command, returncode=0, cached=True, cached_from="01ABC")

    results = pyfltr.command.stage_runner.run_stages(
        ["ran", "skipped", "unresolved", "cached"], make_execution_context(config, [target], start_cwd=tmp_path).base, _execute
    )
    by_command = {result.command: result for result in results}
    ran = by_command["ran"]
    assert ran.started_at is not None
    assert ran.finished_at is not None
    assert ran.started_at <= ran.finished_at
    for command in ("skipped", "unresolved", "cached"):
        assert (by_command[command].started_at, by_command[command].finished_at) == (None, None)


def _custom_formatters(tmp_path: pathlib.Path, *specs: tuple[str, str]) -> pyfltr.config.model.Config:
    body = "".join(
        f'[tool.pyfltr.custom-commands.{name}]\ntype="formatter"\npath="{name}"\ntargets="{targets}"\n'
        for name, targets in specs
    )
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return pyfltr.config.config.load_config(config_dir=tmp_path)


def test_run_stages_runs_idle_commands_without_slot(tmp_path: pathlib.Path) -> None:
    config = _custom_formatters(tmp_path, ("md-only", "*.md"), ("rst-only", "*.rst"))
    target = tmp_path / "input.txt"
    target.write_text("x", encoding="utf-8")
    holding = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with pyfltr.command.execution_slot.hold(label="other"):
            holding.set()
            release.wait(30)

    thread = threading.Thread(target=_holder)
    thread.start()
    waits: list[pyfltr.command.execution_slot.SlotWait] = []
    try:
        assert holding.wait(30)
        results = pyfltr.command.stage_runner.run_stages(
            ["md-only", "rst-only"],
            make_execution_context(config, [target], start_cwd=tmp_path).base,
            lambda command, _fix: make_command_result(command, returncode=None),
            callbacks=pyfltr.command.stage_runner.StageCallbacks(on_slot_wait=waits.append),
        )
    finally:
        release.set()
        thread.join(30)
    assert [result.command for result in results] == ["md-only", "rst-only"]
    assert not waits


def test_slot_wait_reporter_reports_waiting_and_resume() -> None:
    messages: list[str] = []
    notified = threading.Event()

    def notify(message: str) -> None:
        messages.append(message)
        notified.set()

    reporter = pyfltr.command.stage_runner.SlotWaitReporter(notify, lambda: (2, 5), delay=0.0, interval=30.0)
    started = pyfltr.command.execution_slot.SlotWait(kind="initial", sequence=0, started_at="2026-11-10T00:00:00+00:00")
    finished = dataclasses.replace(started, finished_at="2026-11-10T00:00:01+00:00", elapsed=0.3, outcome="acquired")
    # pylint: disable=protected-access  # 実イベントの前後で待機状態の遷移辺を観測する。
    legal = {("NoneType", "_Waiting"), ("_Waiting", "NoneType")}

    def apply(wait: pyfltr.command.execution_slot.SlotWait, expected: str) -> None:
        before = type(reporter._wait).__name__
        reporter(wait)
        after = type(reporter._wait).__name__
        assert before == after or (before, after) in legal
        assert after == expected

    apply(started, "_Waiting")
    try:
        assert notified.wait(5)
    finally:
        apply(finished, "NoneType")
    assert "実行枠の取得を待っています" in messages[0]
    assert "完了したツール: 2/5件" in messages[0]
    assert "残り時間の見込みは" in messages[0]
    assert messages[-1].startswith("実行枠を取得しました")
    count = len(messages)

    reporter._delay = 30.0
    apply(started, "_Waiting")
    apply(started, "_Waiting")
    apply(finished, "NoneType")
    assert len(messages) == count


def _finished_process_owner() -> pyfltr.command.execution_slot.SlotOwner:
    with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
        create_time = psutil.Process(proc.pid).create_time()
        proc.wait(30)
    return pyfltr.command.execution_slot.SlotOwner(
        pid=proc.pid, create_time=create_time, cwd="/tmp/held", acquired_at="2026-11-10T00:00:00+00:00", label="dead"
    )


def test_describe_slot_owner_marks_finished_owner() -> None:
    alive = pyfltr.command.execution_slot.SlotOwner(
        pid=os.getpid(),
        create_time=psutil.Process().create_time(),
        cwd="/tmp/alive",
        acquired_at="2026-11-10T00:00:00+00:00",
        label="tests/a_test.py " * 500,
    )
    assert (
        pyfltr.command.stage_runner.describe_slot_owner(alive)
        == f"pid={os.getpid()}、cwd=/tmp/alive、取得=2026-11-10T00:00:00+00:00"
    )
    dead = _finished_process_owner()
    assert "このプロセスは終了しており" in pyfltr.command.stage_runner.describe_slot_owner(dead)
    assert pyfltr.command.stage_runner.describe_slot_owner(None) == "保持者の記録を読めません"


@pytest.mark.timeout(60)
def test_finished_owner_is_not_reported_as_running(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(pyfltr.command.execution_slot.SLOTS_ENV, "1")
    config = _custom_formatters(tmp_path, ("first", "*.txt"))
    target = tmp_path / "input.txt"
    target.write_text("x", encoding="utf-8")
    monkeypatch.setattr(pyfltr.command.stage_runner, "_SLOT_PROGRESS_DELAY", 0.2)
    monkeypatch.setattr(pyfltr.command.stage_runner, "_SLOT_PROGRESS_INTERVAL", 0.2)
    holding = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with pyfltr.command.execution_slot.hold(label="other"):
            holding.set()
            release.wait(30)

    thread = threading.Thread(target=_holder)
    thread.start()
    progress: list[str] = []
    marked = threading.Event()

    def _on_progress(message: str) -> None:
        progress.append(message)
        if "このプロセスは終了しており" in message:
            marked.set()

    def _release_after_marked() -> None:
        marked.wait(30)
        release.set()

    releaser = threading.Thread(target=_release_after_marked)
    pyfltr.warnings_.clear()
    try:
        assert holding.wait(30)
        owner_path = pyfltr.command.execution_slot.default_lock_dir() / "execution.owner.json"
        owner_path.write_text(json.dumps(dataclasses.asdict(_finished_process_owner())), encoding="utf-8")
        releaser.start()
        results = pyfltr.command.stage_runner.run_stages(
            ["first"],
            make_execution_context(config, [target], start_cwd=tmp_path).base,
            lambda command, _fix: make_command_result(command, returncode=0),
            callbacks=pyfltr.command.stage_runner.StageCallbacks(on_slot_progress=_on_progress),
        )
    finally:
        marked.set()
        release.set()
        if releaser.ident is not None:
            releaser.join(30)
        thread.join(30)
    assert [result.command for result in results] == ["first"]
    warnings = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "execution-slot"]
    assert len(warnings) == 1
    assert "このプロセスは終了しており" in warnings[0]["message"]
    finished_reports = [
        message for message in progress if "実行枠の取得を待っています" in message and "cwd=/tmp/held" in message
    ]
    assert finished_reports
    assert all("このプロセスは終了しており" in message for message in finished_reports), finished_reports
    pyfltr.warnings_.clear()


def _custom_commands(tmp_path: pathlib.Path, *specs: tuple[str, str]) -> pyfltr.config.model.Config:
    body = "".join(
        f'[tool.pyfltr.custom-commands.{name}]\ntype="{kind}"\npath="{name}"\ntargets="*.txt"\n' for name, kind in specs
    )
    (tmp_path / "pyproject.toml").write_text(body, encoding="utf-8")
    return pyfltr.config.config.load_config(config_dir=tmp_path)


def _floor_second(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value).replace(microsecond=0)


@pytest.mark.parametrize("include_fix_stage", [False, True])
def test_run_stages_records_tester_launch_boundary_after_format_second(tmp_path: pathlib.Path, include_fix_stage: bool) -> None:
    config = _custom_commands(
        tmp_path,
        ("fmt", "formatter"),
        ("lint", "linter"),
        ("test-ok", "tester"),
        ("test-failed", "tester"),
        ("test-skipped", "tester"),
        ("test-cached", "tester"),
    )
    target = tmp_path / "input.txt"
    target.write_text("x", encoding="utf-8")

    def _execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        del fix_stage
        if command == "fmt":
            while dt.datetime.now(dt.UTC).microsecond > 200_000:
                time.sleep(0.01)
            return make_command_result(command, returncode=0)
        if command == "test-failed":
            return make_command_result(command, returncode=1)
        if command == "test-skipped":
            return make_command_result(command, returncode=None, files=0)
        if command == "test-cached":
            return make_command_result(command, returncode=0, cached=True, cached_from="01ABC")
        return make_command_result(command, returncode=0)

    results = pyfltr.command.stage_runner.run_stages(
        ["fmt", "lint", "test-ok", "test-failed", "test-skipped", "test-cached"],
        make_execution_context(config, [target], start_cwd=tmp_path).base,
        _execute,
        include_fix_stage=include_fix_stage,
    )
    by_command = {result.command: result for result in results}
    format_end = by_command["fmt"].finished_at
    assert format_end is not None
    for command in ("test-ok", "test-failed"):
        result = by_command[command]
        assert result.launch_boundary_at is not None
        assert dt.datetime.fromisoformat(result.launch_boundary_at) >= _floor_second(format_end) + dt.timedelta(seconds=1)
        assert result.started_at is not None
        assert result.launch_boundary_at <= result.started_at
    for command in ("fmt", "lint", "test-skipped", "test-cached"):
        assert by_command[command].launch_boundary_at is None


def test_run_stages_records_tester_launch_boundary_without_format(tmp_path: pathlib.Path) -> None:
    config = _custom_commands(tmp_path, ("fmt", "formatter"), ("test-ok", "tester"))
    target = tmp_path / "input.txt"
    target.write_text("x", encoding="utf-8")

    def _execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        del fix_stage
        if command == "fmt":
            return make_command_result(command, returncode=None, files=0)
        return make_command_result(command, returncode=0)

    started = time.monotonic()
    results = pyfltr.command.stage_runner.run_stages(
        ["fmt", "test-ok"], make_execution_context(config, [target], start_cwd=tmp_path).base, _execute
    )
    tester = next(result for result in results if result.command == "test-ok")
    assert tester.launch_boundary_at is not None
    assert tester.started_at is not None
    assert tester.launch_boundary_at <= tester.started_at
    assert time.monotonic() - started < 0.5


def test_run_stages_drops_launch_boundary_when_result_is_replaced_by_skip(tmp_path: pathlib.Path) -> None:
    config = _custom_commands(tmp_path, ("test-ok", "tester"))
    target = tmp_path / "input.txt"
    target.write_text("x", encoding="utf-8")
    archived: list[pyfltr.command.core_.CommandResult] = []

    def _execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        del fix_stage
        return make_command_result(command, returncode=0)

    def _replace(result: pyfltr.command.core_.CommandResult, stage: str) -> pyfltr.command.core_.CommandResult:
        del stage
        return pyfltr.command.stage_runner.make_skipped_result(result.command, config, reason="Ctrl+C により中断しました。")

    results = pyfltr.command.stage_runner.run_stages(
        ["test-ok"],
        make_execution_context(config, [target], start_cwd=tmp_path).base,
        _execute,
        callbacks=pyfltr.command.stage_runner.StageCallbacks(replace_result=_replace, archive=archived.append),
    )
    assert [(result.status, result.launch_boundary_at) for result in results] == [("skipped", None)]
    assert [result.launch_boundary_at for result in archived] == [None]
