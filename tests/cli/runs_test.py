import json
import pathlib

import pytest

import pyfltr.cli.main
import pyfltr.cli.output_format
import pyfltr.command.core_
import pyfltr.diagnostics
import pyfltr.parsing.entry
import pyfltr.state.archive
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run


@pytest.fixture(autouse=True)
def _isolated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    """`PYFLTR_CACHE_DIR`を`tmp_path`に固定するフィクスチャ。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_list_runs_text_empty(capsys: pytest.CaptureFixture[str]) -> None:
    returncode = pyfltr.cli.main.run(["list-runs"])
    assert returncode == 0
    captured = capsys.readouterr()
    assert "(no runs)" in captured.out


def test_list_runs_text_multiple(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id1 = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)
    run_id2 = _seed_run(tmp_path, commands=["mypy"], exit_code=1)

    returncode = pyfltr.cli.main.run(["list-runs"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "RUN_ID" in out
    # 新しい順（降順）で並ぶ
    idx1 = out.find(run_id1)
    idx2 = out.find(run_id2)
    assert idx1 >= 0 and idx2 >= 0
    assert idx2 < idx1  # 新しい run_id2 (後から作成) が先


def test_list_runs_limit(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(5):
        _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["list-runs", "--limit", "2"])
    assert returncode == 0
    out = capsys.readouterr().out
    # header行 + 2件 = 3行
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 3


def test_list_runs_json(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path, commands=["ruff-check"], files=2, exit_code=0)

    returncode = pyfltr.cli.main.run(["list-runs", "--output-format=json"])
    assert returncode == 0
    payload = json.loads(capsys.readouterr().out)
    assert "runs" in payload
    assert payload["runs"][0]["run_id"] == run_id
    assert payload["runs"][0]["commands"] == ["ruff-check"]
    assert payload["runs"][0]["files"] == 2
    assert payload["runs"][0]["cwd"] == str(pathlib.Path.cwd())


def test_list_runs_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id1 = _seed_run(tmp_path)
    run_id2 = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["list-runs", "--output-format=jsonl"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert all(line["kind"] == "run" for line in lines)
    # 降順（新しい順）
    assert lines[0]["run_id"] == run_id2
    assert lines[1]["run_id"] == run_id1


def test_show_run_text_overview(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        commands=["ruff-check", "mypy"],
        tool_results=[
            ("ruff-check", 0, "clean", []),
            ("mypy", 1, "error", [_make_error("mypy", "a.py", 1, "boom")]),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id])
    assert returncode == 0
    out = capsys.readouterr().out
    assert f"run_id: {run_id}" in out
    assert "commands:" in out
    assert "ruff-check:" in out
    assert "mypy:" in out
    assert "diagnostics=1" in out


def test_show_run_prefix(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", run_id[:8]])
    assert returncode == 0
    assert f"run_id: {run_id}" in capsys.readouterr().out


def test_show_run_latest(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_run(tmp_path)
    latest_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", "latest"])
    assert returncode == 0
    assert f"run_id: {latest_id}" in capsys.readouterr().out


def test_show_run_prefix_ambiguous(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_ids = [_seed_run(tmp_path) for _ in range(2)]
    # ULIDの先頭は同じタイムスタンプ部分（ミリ秒単位）を共有する可能性が高いため、
    # 実際に共通する最長プレフィックスを算出してテストする。
    shared = 0
    for a, b in zip(run_ids[0], run_ids[1], strict=False):
        if a != b:
            break
        shared += 1
    if shared < 1:
        pytest.skip("shared prefixが無いケースでは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    returncode = pyfltr.cli.main.run(["show-run", prefix])
    assert returncode == 1
    assert "曖昧" in capsys.readouterr().err


def test_show_run_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    returncode = pyfltr.cli.main.run(["show-run", "nonexistent"])
    assert returncode == 1
    err = capsys.readouterr().err
    assert "run_id" in err
    assert "pyfltr list-runs" in err


def test_show_run_latest_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    returncode = pyfltr.cli.main.run(["show-run", "latest"])
    assert returncode == 1
    err = capsys.readouterr().err
    assert "run" in err
    assert "pyfltr run" in err


@pytest.mark.parametrize("env_name", pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)
def test_list_runs_agent_indicator_jsonl(
    env_name: str,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """エージェント検出変数のいずれかが設定されていれば、`list-runs`は--output-format未指定でもJSONLを出力する。"""
    monkeypatch.setenv(env_name, "1")
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["list-runs"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "run"
    assert lines[0]["run_id"] == run_id


@pytest.mark.parametrize("env_name", pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)
def test_show_run_agent_indicator_jsonl(
    env_name: str,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """エージェント検出変数のいずれかが設定されていれば、`show-run`は--output-format未指定でもJSONLを出力する。"""
    monkeypatch.setenv(env_name, "1")
    run_id = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)

    returncode = pyfltr.cli.main.run(["show-run", run_id])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    # JSONL: meta（kind="meta"）+ ツール別command
    assert lines, "JSONLが1行も出ていない"
    assert lines[0]["kind"] == "meta"


def test_show_run_tool_text(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            (
                "mypy",
                1,
                "mypy output",
                [_make_error("mypy", "src/a.py", 42, "型エラー", col=5)],
            ),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "mypy"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "command: mypy" in out
    assert "src/a.py:42:5" in out
    assert "型エラー" in out


def test_show_run_tool_text_renders_hint_urls_and_hints(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`hint_urls`と`hints`がtool.jsonにあるとき`show-run --commands`のtext出力に表示される。"""
    error = pyfltr.diagnostics.ErrorLocation(
        file="src/a.py",
        line=1,
        col=2,
        command="ruff-check",
        message="unused import",
        rule="F401",
        rule_url="https://docs.astral.sh/ruff/rules/F401/",
        hint="Remove the unused import.",
    )
    run_id = _seed_run(
        tmp_path,
        tool_results=[("ruff-check", 1, "out", [error])],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "hint_urls:" in out
    assert "F401: https://docs.astral.sh/ruff/rules/F401/" in out
    assert "hints:" in out
    assert "F401: Remove the unused import." in out


def test_show_run_tool_json(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            (
                "ruff-check",
                1,
                "out",
                [_make_error("ruff-check", "a.py", 1, "msg")],
            ),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check", "--output-format=json"])
    assert returncode == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"]["command"] == "ruff-check"
    assert len(payload["diagnostics"]) == 1
    assert payload["diagnostics"][0]["file"] == "a.py"


def test_show_run_tool_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            (
                "ruff-check",
                1,
                "out",
                [
                    _make_error("ruff-check", "a.py", 1, "msg-a"),
                    _make_error("ruff-check", "b.py", 2, "msg-b"),
                ],
            ),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check", "--output-format=jsonl"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["kind"] == "command"
    assert lines[0]["command"] == "ruff-check"
    assert [line["kind"] for line in lines[1:]] == ["diagnostic", "diagnostic"]
    assert lines[1]["file"] == "a.py"


def test_show_run_tool_not_found(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "nonexistent"])
    assert returncode == 1
    err = capsys.readouterr().err
    assert "nonexistent" in err
    assert "pyfltr show-run" in err


def test_show_run_output_mode(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 1, "raw output line 1\nraw output line 2\n", []),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check", "--output"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "raw output line 1" in out
    assert "raw output line 2" in out


def test_show_run_output_mode_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[("ruff-check", 0, "raw-log", [])],
    )

    returncode = pyfltr.cli.main.run(
        [
            "show-run",
            run_id,
            "--commands",
            "ruff-check",
            "--output",
            "--output-format=jsonl",
        ]
    )
    assert returncode == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "output"
    assert record["command"] == "ruff-check"
    assert record["content"] == "raw-log"


def test_show_run_output_without_tool_errors(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--output"])
    assert returncode == 1
    assert "--commands" in capsys.readouterr().err


def test_show_run_commands_multiple(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 1, "out-r", [_make_error("ruff-check", "a.py", 1, "msg-a")]),
            ("mypy", 1, "out-m", [_make_error("mypy", "b.py", 2, "msg-b")]),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check,mypy"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "command: ruff-check" in out
    assert "command: mypy" in out
    assert "msg-a" in out
    assert "msg-b" in out


def test_show_run_output_with_multiple_commands_errors(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "", []),
            ("mypy", 0, "", []),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check,mypy", "--output"])
    assert returncode == 1
    assert "単一" in capsys.readouterr().err


def test_show_run_jsonl_overview(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "", []),
            ("mypy", 0, "", []),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--output-format=jsonl"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    kinds = [line["kind"] for line in lines]
    assert kinds[0] == "meta"
    assert kinds[1:] == ["timing", "command", "command"]
    assert lines[0]["run_id"] == run_id


@pytest.mark.parametrize("detail", [False, True], ids=["overview", "detail"])
@pytest.mark.parametrize("output_format", ["text", "json", "jsonl"])
def test_show_run_filters_retired_meta_without_hiding_unknown_detail(
    detail: bool,
    output_format: str,
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """旧メタ情報の表示契約を2経路と3形式の組合せで検証する。"""
    run_id = _seed_run(
        tmp_path,
        commands=["ruff-check"],
        tool_results=[("ruff-check", 1, "out", [])],
    )
    meta_path = tmp_path / "runs" / run_id / "tools" / "ruff-check" / "tool.json"
    stored = json.loads(meta_path.read_text(encoding="utf-8"))
    stored["has_error"] = True
    stored["future_key"] = "preserved"
    meta_path.write_text(json.dumps(stored), encoding="utf-8")

    args = ["show-run", run_id, f"--output-format={output_format}"]
    if detail:
        args.extend(["--commands", "ruff-check"])
    returncode = pyfltr.cli.main.run(args)

    assert returncode == 0
    output = capsys.readouterr().out
    assert "has_error" not in output
    if output_format == "text" or not detail:
        assert "future_key" not in output
    else:
        assert "future_key" in output


def _seed_timed_run(cache_root: pathlib.Path, *, slot_waits: list | None, finished: bool = True) -> str:
    """待機区間と本体区間を持つrunを保存する。`slot_waits=None`は区間を計測しない版の記録を模す。"""
    store = pyfltr.state.archive.ArchiveStore(cache_root=cache_root)
    run_id = store.start_run(commands=["mypy"], files=1)
    result = pyfltr.command.core_.CommandResult(
        command="mypy", command_type="linter", commandline=[], returncode=0, files=1, output="", elapsed=1.5
    )
    result.started_at = "2026-11-05T00:00:02+00:00"
    result.finished_at = "2026-11-05T00:00:03.5+00:00"
    store.write_tool_result(run_id, result)
    meta_path = cache_root / "runs" / run_id / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["started_at"] = "2026-11-05T00:00:00+00:00"
    if finished:
        meta["finished_at"] = "2026-11-05T00:00:04+00:00"
        meta["exit_code"] = 0
    if slot_waits is None:
        del meta["slot_waits"]
    else:
        meta["slot_waits"] = slot_waits
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    store.release_run(run_id)
    return run_id


_FINISHED_WAIT = {
    "kind": "initial",
    "sequence": 0,
    "started_at": "2026-11-05T00:00:00+00:00",
    "finished_at": "2026-11-05T00:00:02+00:00",
    "elapsed": 2.0,
    "outcome": "acquired",
}
_UNFINISHED_WAIT = {
    "kind": "reacquire",
    "sequence": 1,
    "started_at": "2026-11-05T00:00:03.5+00:00",
    "finished_at": None,
    "elapsed": None,
    "outcome": None,
}


def test_show_run_timing_json_and_jsonl(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """待機区間・本体区間・run全体の経過を対応付けて返す。"""
    run_id = _seed_timed_run(tmp_path, slot_waits=[_FINISHED_WAIT])

    assert pyfltr.cli.main.run(["show-run", run_id, "--output-format=json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["timing"] == {
        "measured": True,
        "run_elapsed": 4.0,
        "slot_waits": [_FINISHED_WAIT],
        "slot_wait_total": 2.0,
        "unfinished_slot_waits": 0,
    }
    command = payload["commands"][0]
    assert (command["elapsed"], command["started_at"], command["finished_at"]) == (
        1.5,
        "2026-11-05T00:00:02+00:00",
        "2026-11-05T00:00:03.5+00:00",
    )

    assert pyfltr.cli.main.run(["show-run", run_id, "--output-format=jsonl"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [line["kind"] for line in lines] == ["meta", "timing", "command"]
    assert lines[1]["slot_wait_total"] == 2.0


@pytest.mark.parametrize(
    ("slot_waits", "finished", "expected_lines"),
    [
        pytest.param([_FINISHED_WAIT], True, ["elapsed: 4.000s", "slot_waits: 2.000s", "initial#0: elapsed=2.000s"], id="done"),
        pytest.param(
            [_FINISHED_WAIT, _UNFINISHED_WAIT],
            False,
            ["elapsed: unfinished", "slot_waits: unfinished", "reacquire#1: elapsed=unfinished outcome=unfinished"],
            id="unfinished",
        ),
        pytest.param(None, True, ["elapsed: 4.000s", "slot_waits: not measured"], id="not-measured"),
    ],
)
def test_show_run_timing_text(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    slot_waits: list | None,
    finished: bool,
    expected_lines: list[str],
) -> None:
    """textは未確定を`unfinished`、未計測を`not measured`と表示し、0秒と区別する。"""
    run_id = _seed_timed_run(tmp_path, slot_waits=slot_waits, finished=finished)
    assert pyfltr.cli.main.run(["show-run", run_id, "--output-format=text"]) == 0
    out = capsys.readouterr().out
    for line in expected_lines:
        assert line in out
    assert "mypy: status=succeeded diagnostics=0 elapsed=1.500s" in out
    assert "0.000s" not in out


def test_show_run_tool_text_shows_body_interval(tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--commands`のtextは本体区間の時刻を表示する。"""
    run_id = _seed_timed_run(tmp_path, slot_waits=[])
    assert pyfltr.cli.main.run(["show-run", run_id, "--commands=mypy", "--output-format=text"]) == 0
    out = capsys.readouterr().out
    assert "started_at: 2026-11-05T00:00:02+00:00" in out
    assert "finished_at: 2026-11-05T00:00:03.5+00:00" in out
