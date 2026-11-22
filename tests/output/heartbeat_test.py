import time

import pyfltr.cli.main
import pyfltr.cli.pipeline
import pyfltr.config.config
import pyfltr.output.heartbeat
import pyfltr.run_options


def test_heartbeat_emits_running_for_active_commands() -> None:
    emitted_running: list[tuple[str, float]] = []
    emitted_text: list[str] = []
    last_output_time = [time.monotonic() - 1.0]

    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=0.1,
        tick_interval=0.05,
        emit_running=lambda command, elapsed: emitted_running.append((command, elapsed)),
        emit_text=emitted_text.append,
        get_last_output_time=lambda: last_output_time[0],
        set_last_output_time=lambda value: last_output_time.__setitem__(0, value),
    )

    monitor.on_command_start("pytest")
    monitor.on_command_start("mypy")

    monitor.tick(time.monotonic())

    commands_emitted = sorted(name for name, _ in emitted_running)
    assert commands_emitted == ["mypy", "pytest"]
    assert len(emitted_text) == 2  # noqa: PLR2004
    for message in emitted_text:
        assert "running for" in message
        assert "no JSONL output for" in message


def test_heartbeat_does_not_emit_when_silence_below_threshold() -> None:
    emitted_running: list[tuple[str, float]] = []
    last_output_time = [time.monotonic()]

    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=10.0,
        tick_interval=1.0,
        emit_running=lambda command, elapsed: emitted_running.append((command, elapsed)),
        emit_text=lambda message: None,
        get_last_output_time=lambda: last_output_time[0],
        set_last_output_time=lambda value: last_output_time.__setitem__(0, value),
    )
    monitor.on_command_start("pytest")
    monitor.tick(time.monotonic())

    assert not emitted_running


def test_heartbeat_skips_emission_when_no_running_commands() -> None:
    emitted_running: list[tuple[str, float]] = []
    last_output_time = [time.monotonic() - 1.0]

    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=0.1,
        tick_interval=0.05,
        emit_running=lambda command, elapsed: emitted_running.append((command, elapsed)),
        emit_text=lambda message: None,
        get_last_output_time=lambda: last_output_time[0],
        set_last_output_time=lambda value: last_output_time.__setitem__(0, value),
    )
    monitor.tick(time.monotonic())

    assert not emitted_running
    assert last_output_time[0] >= time.monotonic() - 0.5


def test_heartbeat_on_command_end_removes_from_running_set() -> None:
    emitted_running: list[tuple[str, float]] = []
    last_output_time = [time.monotonic() - 1.0]

    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=0.1,
        tick_interval=0.05,
        emit_running=lambda command, elapsed: emitted_running.append((command, elapsed)),
        emit_text=lambda message: None,
        get_last_output_time=lambda: last_output_time[0],
        set_last_output_time=lambda value: last_output_time.__setitem__(0, value),
    )
    monitor.on_command_start("pytest")
    monitor.on_command_end("pytest")
    monitor.tick(time.monotonic())

    assert not emitted_running


def test_heartbeat_thread_lifecycle_starts_and_stops() -> None:
    emitted_running: list[tuple[str, float]] = []
    last_output_time = [time.monotonic() - 10.0]

    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=0.05,
        tick_interval=0.05,
        emit_running=lambda command, elapsed: emitted_running.append((command, elapsed)),
        emit_text=lambda message: None,
        get_last_output_time=lambda: last_output_time[0],
        set_last_output_time=lambda value: last_output_time.__setitem__(0, value),
    )
    monitor.on_command_start("pytest")
    monitor.start()
    deadline = time.monotonic() + 30.0
    while not emitted_running and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()

    assert len(emitted_running) >= 1
    assert emitted_running[0][0] == "pytest"
