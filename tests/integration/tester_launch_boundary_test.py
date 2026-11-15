"""テスターの開始境界を、公開runの実起動からshow-runとMCPの取得まで通して検証する。"""

import datetime as dt
import json
import pathlib
import sys

import pytest
import tomlkit

import pyfltr.cli.main
import pyfltr.cli.mcp_runs_tools

# 秒の切り替わり直後に整形を終え、同じ秒のうちに試験へ進む入力を作るformatter。
_FORMATTER = """\
import datetime, pathlib, sys, time
while datetime.datetime.now(datetime.UTC).microsecond > 200_000:
    time.sleep(0.01)
for path in sys.argv[1:]:
    pathlib.Path(path).write_text("formatted\\n", encoding="utf-8")
"""
# 起動した時刻を書き出し、引数の終了コードで終わるテスター。
_TESTER = """\
import datetime, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(datetime.datetime.now(datetime.UTC).isoformat(), encoding="utf-8")
sys.exit(int(sys.argv[2]))
"""


def _tester(script: pathlib.Path, started: pathlib.Path, returncode: int, targets: str) -> dict:
    return {"type": "tester", "path": sys.executable, "args": [str(script), str(started), str(returncode)], "targets": targets}


@pytest.mark.asyncio
async def test_public_run_records_launch_boundary_after_format_second(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """整形と同じ秒に試験を始める入力でも、境界は整形の秒より後で、テスターの実起動より前になる。

    起動を試みたテスター（成功・失敗・実行ファイルが無い起動失敗）だけに記録し、対象なしには記録しない。
    起動失敗は`failed`のため、成功した実開始の証拠にはならない。
    show-runとMCPのshow_runは同じ値を返す。
    """
    project = tmp_path / "project"
    project.mkdir()
    formatter = project / "fmt.py"
    formatter.write_text(_FORMATTER, encoding="utf-8")
    tester = project / "tester.py"
    tester.write_text(_TESTER, encoding="utf-8")
    (project / "input.txt").write_text("x\n", encoding="utf-8")
    started_ok = tmp_path / "started-ok.txt"
    started_failed = tmp_path / "started-failed.txt"
    commands = {
        "fmt": {"type": "formatter", "path": sys.executable, "args": [str(formatter)], "targets": "*.txt"},
        "test-ok": _tester(tester, started_ok, 0, "*.txt"),
        "test-failed": _tester(tester, started_failed, 1, "*.txt"),
        "test-no-target": _tester(tester, tmp_path / "unused.txt", 0, "*.none"),
        "test-missing-exe": {"type": "tester", "path": "pyfltr-missing-command-for-test", "targets": "*.txt"},
    }
    settings = {"custom-commands": commands}
    (project / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"pyfltr": settings}}), encoding="utf-8")
    monkeypatch.chdir(project)

    pyfltr.cli.main.run(
        ["run", f"--commands={','.join(commands)}", "--no-cache", "--output-format=jsonl", "--no-quiet", "input.txt"]
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    run_id = next(record["run_id"] for record in records if record["kind"] == "header")

    assert pyfltr.cli.main.run(["show-run", run_id, "--output-format=json"]) == 0
    by_command = {command["command"]: command for command in json.loads(capsys.readouterr().out)["commands"]}
    overview = await pyfltr.cli.mcp_runs_tools.tool_show_run(run_id)
    mcp_by_command = {command.command: command for command in overview.commands}

    format_end = dt.datetime.fromisoformat(by_command["fmt"]["finished_at"])
    for command, started_file in (("test-ok", started_ok), ("test-failed", started_failed)):
        boundary = by_command[command]["launch_boundary_at"]
        assert mcp_by_command[command].launch_boundary_at == boundary
        boundary_time = dt.datetime.fromisoformat(boundary)
        assert boundary_time >= format_end.replace(microsecond=0) + dt.timedelta(seconds=1)
        assert boundary_time <= dt.datetime.fromisoformat(started_file.read_text(encoding="utf-8"))
    assert (by_command["test-ok"]["status"], by_command["test-failed"]["status"]) == ("succeeded", "failed")
    missing = by_command["test-missing-exe"]
    assert (missing["status"], missing["launch_boundary_at"]) == (
        "failed",
        mcp_by_command["test-missing-exe"].launch_boundary_at,
    )
    serialized = {command["command"]: command for command in overview.model_dump(mode="json")["commands"]}
    for command in ("fmt", "test-no-target"):
        assert "launch_boundary_at" not in by_command[command]
        assert "launch_boundary_at" not in serialized[command]
    assert by_command["test-no-target"]["status"] == "skipped"
