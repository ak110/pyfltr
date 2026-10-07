import json
import pathlib
import time

import pyfltr.cli.pipeline
import pyfltr.config.config
import pyfltr.run_options


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


def _make_args(tmp_path: pathlib.Path, *, commands: list[str] | None) -> pyfltr.run_options.RunOptions:
    """警告検証用の最小引数を生成する。"""
    target = tmp_path / "sample.md"
    target.write_text("# Title\n", encoding="utf-8")
    return pyfltr.run_options.RunOptions.from_values(
        {
            "output_format": "jsonl",
            "output_file": None,
            "format_source": None,
            "stream": False,
            "no_clear": True,
            "no_ui": True,
            "ui": False,
            "targets": [target],
            "commands": commands,
            "exit_zero_even_if_formatted": True,
            "include_fix_stage": False,
            "fail_fast": False,
            "jobs": None,
            "no_exclude": False,
            "no_gitignore": True,
            "allow_external_paths": False,
            "human_readable": False,
            "shuffle": False,
            "verbose": False,
            "ci": False,
            "only_failed": False,
            "from_run": None,
            "changed_since": None,
            "no_archive": True,
            "no_cache": True,
            "subcommand": "run",
        }
    )


def _run_and_read_jsonl(
    monkeypatch,
    capsys,
    tmp_path: pathlib.Path,
    commands: list[str],
    *,
    args_commands: list[str] | None,
    enabled: set[str] | None = None,
) -> list[dict]:
    """パイプラインを実ツール起動なしで実行し、JSONLレコードを返す。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pyfltr.cli.pipeline.run_commands_with_cli", lambda *args, **kwargs: [])
    config = pyfltr.config.config.create_default_config()
    config.values["respect-gitignore"] = False
    config.values["subproject-use-gitignore"] = False
    for command in enabled or set():
        config.values[command] = True

    pyfltr.cli.pipeline.run_pipeline(_make_args(tmp_path, commands=args_commands), commands, config)

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    for record in records:
        if record.get("kind") == "warning":
            assert record["msg"] not in captured.err
    return records


def _summary_warning_count(records: list[dict]) -> int:
    """summaryレコードの`warnings`値を返す（キー不在は0件とみなす）。"""
    summary = next(record for record in records if record.get("kind") == "summary")
    return summary.get("warnings", 0)


def test_unmet_commands_warning_emitted_when_commands_explicit(monkeypatch, capsys, tmp_path) -> None:
    """明示指定された未有効化コマンドはJSONL警告として出力される。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint"],
        args_commands=["textlint"],
    )

    warnings = [record for record in records if record.get("kind") == "warning"]
    assert warnings == [
        {
            "kind": "warning",
            "source": "commands",
            "msg": "--commandsで指定されたが有効化されていないため未実行のコマンドがあります: textlint",
            "hint": (
                "--enable=textlint または pyproject.toml [tool.pyfltr] で指定したコマンドを true に設定して有効化してください。"
            ),
        }
    ]
    assert _summary_warning_count(records) == len(warnings)


def test_unmet_commands_warning_not_emitted_without_explicit_commands(monkeypatch, capsys, tmp_path) -> None:
    """`--commands`未指定時は未有効化コマンド警告を出力しない。"""
    records = _run_and_read_jsonl(monkeypatch, capsys, tmp_path, ["textlint"], args_commands=None)

    assert not [record for record in records if record.get("source") == "commands"]
    warnings = [record for record in records if record.get("kind") == "warning"]
    assert _summary_warning_count(records) == len(warnings)


def test_unmet_commands_warning_not_emitted_when_all_enabled(monkeypatch, capsys, tmp_path) -> None:
    """明示指定コマンドがすべて有効な場合は警告を出力しない。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint"],
        args_commands=["textlint"],
        enabled={"textlint"},
    )

    assert not [record for record in records if record.get("source") == "commands"]


def test_unmet_commands_warning_groups_multiple_commands(monkeypatch, capsys, tmp_path) -> None:
    """複数の未有効化コマンドは1件の警告にまとめる。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint", "markdownlint", "typos"],
        args_commands=["textlint,markdownlint,typos"],
        enabled={"typos"},
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert warning["msg"].endswith("textlint, markdownlint")
    assert warning["hint"].startswith("--enable=textlint,markdownlint")


def test_unmet_commands_warning_not_emitted_for_alias_with_any_enabled(monkeypatch, capsys, tmp_path) -> None:
    """エイリアス指定で展開結果に有効化済みが1件以上あれば警告を出力しない。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit"],
        enabled={"pnpm-audit"},
    )

    assert not [record for record in records if record.get("source") == "commands"]


def test_unmet_commands_warning_emitted_for_alias_with_none_enabled(monkeypatch, capsys, tmp_path) -> None:
    """エイリアス指定で展開結果が全て未有効化なら従来どおり警告を出力する。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit"],
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert "uv-audit" in warning["msg"]
    assert "yarn-audit" in warning["msg"]


def test_unmet_commands_warning_emitted_for_explicit_command_with_alias(monkeypatch, capsys, tmp_path) -> None:
    """エイリアスと個別コマンド名の併記では、個別指定した未有効化コマンドの警告を出力する。"""
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit,uv-audit"],
        enabled={"pnpm-audit"},
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert warning["msg"].endswith("uv-audit")
