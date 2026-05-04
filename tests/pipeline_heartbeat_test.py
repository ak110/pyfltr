"""パイプラインheartbeat監視（`HeartbeatMonitor`）の動作テスト。

JSONL出力経路をモックし、最後の出力からの経過時間がしきい値を超えたとき、
実行中コマンドそれぞれに`status:"running"`レコードが期待通り発行されることを確認する。
heartbeat_interval等は固定値注入で1秒以内に収まる構成にしている。
"""

import time

import pyfltr.cli.pipeline


def test_heartbeat_emits_running_for_active_commands() -> None:
    """しきい値超過時に実行中コマンド全件に対してrunningイベントが発行される。"""
    emitted_running: list[tuple[str, float]] = []
    emitted_text: list[str] = []
    last_output_time = [time.monotonic() - 1.0]  # 既にしきい値超過の状態でtickを呼ぶ

    monitor = pyfltr.cli.pipeline.HeartbeatMonitor(
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

    monitor = pyfltr.cli.pipeline.HeartbeatMonitor(
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

    monitor = pyfltr.cli.pipeline.HeartbeatMonitor(
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

    monitor = pyfltr.cli.pipeline.HeartbeatMonitor(
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

    monitor = pyfltr.cli.pipeline.HeartbeatMonitor(
        threshold=0.05,
        tick_interval=0.05,
        emit_running=lambda command, elapsed: emitted_running.append((command, elapsed)),
        emit_text=lambda message: None,
        get_last_output_time=lambda: last_output_time[0],
        set_last_output_time=lambda value: last_output_time.__setitem__(0, value),
    )
    monitor.on_command_start("pytest")
    monitor.start()
    # tickが少なくとも1回実行されるのを待つ。tick_intervalの2倍程度sleepすれば確実。
    time.sleep(0.2)
    monitor.stop()

    # 1回以上のheartbeatが発行されている。連続発火は抑止される設計のため、件数は1〜数件で揺れる。
    assert len(emitted_running) >= 1
    assert emitted_running[0][0] == "pytest"
