import time

import pyfltr.cli.main
import pyfltr.cli.pipeline
import pyfltr.config.config
import pyfltr.output.heartbeat
import pyfltr.run_options


def test_heartbeat_emits_running_for_active_commands() -> None:
    """しきい値超過時に実行中コマンド全件に対してrunningイベントが発行される。"""
    emitted_running: list[tuple[str, float]] = []
    emitted_text: list[str] = []
    last_output_time = [time.monotonic() - 1.0]  # 既にしきい値超過の状態でtickを呼ぶ

    monitor = pyfltr.output.heartbeat.HeartbeatMonitor(
        threshold=0.1,
        tick_interval=0.05,
        emit_running=lambda command, elapsed: emitted_running.append((command, elapsed)),
        emit_text=emitted_text.append,
        get_last_output_time=lambda: last_output_time[0],
        set_last_output_time=lambda value: last_output_time.__setitem__(0, value),
    )

    # 実行中コマンドを2件登録
    monitor.on_command_start("pytest")
    monitor.on_command_start("mypy")

    # tickをマニュアルで呼んで判定実行
    monitor.tick(time.monotonic())

    # 2件のrunningイベントが発行され、textも2件出力される
    commands_emitted = sorted(name for name, _ in emitted_running)
    assert commands_emitted == ["mypy", "pytest"]
    assert len(emitted_text) == 2  # noqa: PLR2004
    for message in emitted_text:
        assert "running for" in message
        assert "no JSONL output for" in message


def test_heartbeat_does_not_emit_when_silence_below_threshold() -> None:
    """しきい値未満の無音時間ではrunningイベントを発行しない。"""
    emitted_running: list[tuple[str, float]] = []
    last_output_time = [time.monotonic()]  # 直前に出力したばかり

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
    """実行中コマンドが空ならrunningイベントは発行しない。"""
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
    # コマンド未登録のままtick
    monitor.tick(time.monotonic())

    assert not emitted_running
    # 連続発火抑止のためlast_output_timeは更新されている
    assert last_output_time[0] >= time.monotonic() - 0.5


def test_heartbeat_on_command_end_removes_from_running_set() -> None:
    """`on_command_end` が実行中コマンド集合から削除し、以後heartbeat対象外になる。"""
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
    """`start()` / `stop()` で監視スレッドのライフサイクルを管理できる。"""
    emitted_running: list[tuple[str, float]] = []
    last_output_time = [time.monotonic() - 10.0]  # 大幅に古い時刻

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
    # tickがheartbeatを1回以上発行したことを観測してから停止する。上限内に観測できなければ失敗させる。
    deadline = time.monotonic() + 30.0
    while not emitted_running and time.monotonic() < deadline:
        time.sleep(0.01)
    monitor.stop()

    # 1回以上のheartbeatが発行されている。連続発火は抑止される設計のため、件数は1〜数件で揺れる。
    assert len(emitted_running) >= 1
    assert emitted_running[0][0] == "pytest"
