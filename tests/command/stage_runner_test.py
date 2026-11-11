import concurrent.futures
import dataclasses
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
    """待機者が現れても途中では譲らず、全ツールの回収後に枠を解放する。"""
    # 待機者を作るため枠を1つにする。
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
    """戻り値が CommandResult 型であること。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("mypy", config)
    assert isinstance(result, pyfltr.command.core_.CommandResult)


def test_make_skipped_result_status_is_skipped():
    """status が "skipped" であること（returncode=None が条件）。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("mypy", config)
    assert result.status == "skipped"
    assert result.returncode is None


def test_make_skipped_result_preserves_command_name():
    """command 名が保持されること。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("ruff-check", config)
    assert result.command == "ruff-check"


def test_make_skipped_result_is_not_failed():
    """skippedは失敗扱いしない。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.stage_runner.make_skipped_result("mypy", config)
    assert result.failed is False


def _make_pending_future() -> concurrent.futures.Future:
    """cancel 可能な pending 状態の Future を生成する。"""
    # Future を直接インスタンス化し、set_result も cancel() も呼ばなければ pending 状態になる。
    f: concurrent.futures.Future = concurrent.futures.Future()
    return f


def _make_done_future() -> concurrent.futures.Future:
    """完了済みの Future を生成する。"""
    f: concurrent.futures.Future = concurrent.futures.Future()
    f.set_result(None)
    return f


def test_cancel_pending_futures_cancels_pending_only():
    """done でない future だけが cancel() され、done な future は触らない。"""
    aborted: set[str] = set()

    done_future = _make_done_future()
    pending_future = _make_pending_future()

    future_to_command = {
        done_future: "done-cmd",
        pending_future: "pending-cmd",
    }

    pyfltr.command.stage_runner.cancel_pending_futures(future_to_command, aborted)

    # cancel が成功した future のコマンドだけ aborted に入る
    assert "done-cmd" not in aborted
    assert "pending-cmd" in aborted


def test_cancel_pending_futures_adds_cancelled_commands():
    """cancel() が成功した future のコマンド名が aborted_commands に追加される。"""
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
    """cancel() が False を返した（既に running/done）future は aborted_commands に入らない。"""
    aborted: set[str] = set()

    # running 状態（running=True で cancel 不可）を模倣するため、
    # ThreadPoolExecutor で実行中にする
    started = threading.Event()
    can_finish = threading.Event()

    def _blocking_task():
        started.set()
        can_finish.wait()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        running_future = executor.submit(_blocking_task)
        started.wait()  # running 状態になるまで待つ

        future_to_command = {running_future: "running-cmd"}
        pyfltr.command.stage_runner.cancel_pending_futures(future_to_command, aborted)

        can_finish.set()

    # running future は cancel() が False を返すため aborted に入らない
    assert "running-cmd" not in aborted


def _run_fail_fast_stage(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, run_on_submit: set[str]
) -> tuple[dict[str, str], list[str]]:
    """失敗するlinterと後続のlinterを`fail_fast=True`で並列ステージへ渡し、状態と実行順を返す。"""
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
    """失敗を回収する時点で未開始の後続はキャンセルされ、実行されずにskippedとなる。"""
    statuses, executed = _run_fail_fast_stage(tmp_path, monkeypatch, run_on_submit={"failing"})

    assert statuses == {"failing": "failed", "follow-up": "skipped"}
    assert executed == ["failing"]


def test_run_stages_fail_fast_keeps_completed_follow_up(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """失敗を回収する時点で完了済みの後続は元の結果を保ち、skippedへ書き換えない。"""
    statuses, executed = _run_fail_fast_stage(tmp_path, monkeypatch, run_on_submit={"failing", "follow-up"})

    assert statuses == {"failing": "failed", "follow-up": "succeeded"}
    assert sorted(executed) == ["failing", "follow-up"]


def _run_fix_failure_stages(
    tmp_path: pathlib.Path,
    *,
    fail_fast: bool,
    normal_returncode: int | None,
) -> tuple[list[pyfltr.command.core_.CommandResult], list[str], list[str]]:
    """fix段が失敗するlinterを実行し、最終結果・`on_result`・`archive`へ渡ったコマンド名を返す。"""
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
    """fix段の失敗は通常段の成功で消えず、最終結果・通知・保存が同じ1件の失敗になる。"""
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
    """fail-fastで停止してもfix段の失敗を記録し、同じツールのスキップ結果を重ねない。"""
    results, notified, archived = _run_fix_failure_stages(tmp_path, fail_fast=True, normal_returncode=0)

    statuses = {result.command: result.status for result in results}
    assert statuses == {"fixable": "failed", "follow-up": "skipped"}
    assert [result.command for result in results].count("fixable") == 1
    assert notified.count("fixable:failed") == 1
    assert archived.count("fixable:failed") == 1


def test_merge_fix_failure_keeps_fix_diagnostics_when_normal_stage_has_none() -> None:
    """通常段が診断を出力しない（スキップ・診断0件）場合はfix段の診断を最終結果へ残す。

    fail-fastで通常段がスキップされた場合などに、失敗を指すファイルと位置が失われないことを確かめる。
    """
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
    """通常段が同じ違反の診断を出力した場合は通常段の診断だけを使い、重複させない。"""
    diagnostic = pyfltr.diagnostics.ErrorLocation(file="x.py", line=1, col=None, command="ruff-check", message="F821")
    fix_result = make_command_result("ruff-check", returncode=1, errors=[diagnostic])
    normal_result = make_command_result("ruff-check", returncode=1, errors=[diagnostic])

    merged = pyfltr.command.stage_runner.merge_fix_failure(fix_result, normal_result)

    assert merged.status == "failed"
    assert merged.errors == [diagnostic]


def test_run_stages_sets_body_interval_only_for_executed_results(tmp_path: pathlib.Path) -> None:
    """プロセスを起動した結果だけに本体区間の時刻を付け、起動しなかったスキップとキャッシュ復元には付けない。"""
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
            # 起動コマンドの解決失敗は`returncode=1`を持つが、プロセスは起動していない。
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
    """対象を持ち得ないツールだけの実行は、別の保持者がいても実行枠を待たずに完了する。"""
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
    """閾値を超えた待機の継続と再開を通知し、短い待機は通知しない。"""
    messages: list[str] = []
    reporter = pyfltr.command.stage_runner.SlotWaitReporter(messages.append, lambda: (2, 5), delay=0.05, interval=0.05)
    started = pyfltr.command.execution_slot.SlotWait(kind="initial", sequence=0, started_at="2026-11-10T00:00:00+00:00")
    reporter(started)
    time.sleep(0.3)
    reporter(
        pyfltr.command.execution_slot.SlotWait(
            kind="initial",
            sequence=0,
            started_at=started.started_at,
            finished_at="2026-11-10T00:00:01+00:00",
            elapsed=0.3,
            outcome="acquired",
        )
    )
    assert len(messages) >= 3
    assert "実行枠の取得を待っています" in messages[0]
    assert "完了したツール: 2/5件" in messages[0]
    assert "残り時間の見込みは" in messages[0]
    assert messages[-1].startswith("実行枠を取得しました")
    count = len(messages)

    reporter(pyfltr.command.execution_slot.SlotWait(kind="initial", sequence=0, started_at="2026-11-10T00:00:02+00:00"))
    reporter(
        pyfltr.command.execution_slot.SlotWait(
            kind="initial",
            sequence=0,
            started_at="2026-11-10T00:00:02+00:00",
            finished_at="2026-11-10T00:00:02+00:00",
            elapsed=0.0,
            outcome="acquired",
        )
    )
    assert len(messages) == count


def _finished_process_owner() -> pyfltr.command.execution_slot.SlotOwner:
    """終了済みのプロセスを指す保持者記録を作る（強制終了した保持者の記録が残った状態の再現）。"""
    with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
        create_time = psutil.Process(proc.pid).create_time()
        proc.wait(30)
    return pyfltr.command.execution_slot.SlotOwner(
        pid=proc.pid, create_time=create_time, cwd="/tmp/held", acquired_at="2026-11-10T00:00:00+00:00", label="dead"
    )


def test_describe_slot_owner_marks_finished_owner() -> None:
    """終了済みの保持者記録には終了済みである旨を併記し、稼働中の保持者には付けない。"""
    alive = pyfltr.command.execution_slot.SlotOwner(
        pid=os.getpid(),
        create_time=psutil.Process().create_time(),
        cwd="/tmp/alive",
        acquired_at="2026-11-10T00:00:00+00:00",
    )
    assert pyfltr.command.stage_runner.describe_slot_owner(alive) == f"pid={os.getpid()}、cwd=/tmp/alive"
    dead = _finished_process_owner()
    assert "このプロセスは終了しており" in pyfltr.command.stage_runner.describe_slot_owner(dead)
    assert pyfltr.command.stage_runner.describe_slot_owner(None) == "保持者の記録を読めません"


@pytest.mark.timeout(60)
def test_finished_owner_is_not_reported_as_running(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """保持者記録が終了済みのプロセスを指す間は、初回の待機警告と継続表示の両方で稼働中と表示しない。

    強制終了した保持者の記録が残った状態を、保持中の記録ファイルを終了済みのプロセスの記録へ置き換えて再現する。
    待機を起こすため枠を1つにする。
    """
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

    # 終了済みの記録での継続表示を観測してから解放する。固定時間で解放すると、
    # 負荷の高い環境では継続表示の前に解放され、観測できないまま終わる。
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
    waiting = [message for message in progress if "実行枠の取得を待っています" in message]
    assert any("このプロセスは終了しており" in message for message in waiting)
    # 解放の直後は保持者記録が空になり「読めません」と表示されうる。どの時点でも稼働中の保持者として表示しない。
    assert all("このプロセスは終了しており" in message or "保持者の記録を読めません" in message for message in waiting), waiting
    pyfltr.warnings_.clear()
