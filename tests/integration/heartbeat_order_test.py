"""heartbeatと最終JSONLの競合をイベントで制御する。"""

import io
import json
import threading
import time
import typing

import pytest

import pyfltr.cli.pipeline
import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.config.config
import pyfltr.output.heartbeat
import pyfltr.output.jsonl_emit
import pyfltr.output.logging_
from tests import conftest as testconf


def test_final_result_between_snapshot_and_emission(monkeypatch: pytest.MonkeyPatch) -> None:
    """snapshotが古くても最終JSONLの後にrunningを発行しない。"""
    output = io.StringIO()
    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=0,
        get_last_output_time=lambda: 0,
        emit_text=lambda _: None,
    )
    monitor.on_command_start("pytest")
    # snapshot取得後と発行前の競合を固定するため、監視内部の観測点を置換する。
    original_snapshot = monitor._snapshot_running  # pylint: disable=protected-access

    def execute(
        _command: str, _args: typing.Any, context: pyfltr.command.core_.ExecutionContext
    ) -> pyfltr.command.core_.CommandResult:
        assert context.on_subprocess_start is not None
        assert context.on_subprocess_end is not None
        context.on_subprocess_start()
        context.on_subprocess_end()
        return pyfltr.command.core_.CommandResult(
            command="pytest",
            command_type="tester",
            commandline=[],
            returncode=0,
            files=1,
            output="",
            elapsed=0,
        )

    monkeypatch.setattr(pyfltr.command.dispatcher, "execute_command", execute)
    config = pyfltr.config.config.create_default_config()
    config.values["pytest"] = True

    def emit(_result: pyfltr.command.core_.CommandResult) -> None:
        pyfltr.output.jsonl_emit.emit_record('{"kind":"command","command":"pytest","status":"succeeded"}')

    def snapshot() -> list[tuple[str, float]]:
        result = original_snapshot()
        pyfltr.cli.pipeline.run_commands_with_cli(
            ["pytest"],
            testconf.make_args(),
            testconf.make_execution_context(config, []).base,
            per_command_log=False,
            heartbeat=monitor,
            on_result=emit,
        )
        return result

    monkeypatch.setattr(monitor, "_snapshot_running", snapshot)
    pyfltr.output.logging_.configure_structured_output(output)
    try:
        monitor.tick(time.monotonic())
        monkeypatch.setattr(monitor, "_snapshot_running", original_snapshot)
        monitor.on_command_start("pytest")
        monitor.on_command_end("pytest")
        monitor.tick(time.monotonic())
    finally:
        pyfltr.output.logging_.configure_structured_output(None)
    assert [json.loads(line)["status"] for line in output.getvalue().splitlines()] == ["succeeded"]


def test_stop_waits_for_emission_to_finish() -> None:
    """停止要求の時点で発行中なら、停止は実スレッドの終了まで戻らない。"""
    emitting = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    def emit(_command: str, _elapsed: float) -> None:
        emitting.set()
        assert release.wait(10)

    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=0,
        tick_interval=0.001,
        get_last_output_time=lambda: 0,
        emit_running=emit,
        emit_text=lambda _: None,
    )

    def stop() -> None:
        monitor.stop()
        stopped.set()

    monitor.on_command_start("pytest")
    monitor.start()
    thread = threading.Thread(target=stop)
    try:
        assert emitting.wait(10)
        thread.start()
        # 停止要求とjoin完了を区別して観測する。
        assert monitor._stop_event.wait(10)  # pylint: disable=protected-access
        assert not stopped.wait(0.05)
    finally:
        release.set()
        if thread.ident is not None:
            thread.join(10)
        monitor.stop()
    assert stopped.is_set()
    assert not thread.is_alive()
