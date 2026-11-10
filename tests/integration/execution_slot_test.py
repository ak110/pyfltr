"""別プロセスの検証が実行枠を共有し、ツールの実行区間が重ならないことの結合試験。

各試験は先発の実行（pyfltrまたは実行枠のAPIを使う保持者）がツールを実行中の間に後発の実行を起動する。
text出力の後発は待機の警告を標準エラーへ即時に出力するため、その出力を同期点にして先発を解放する。
"""

import datetime as dt
import json
import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

import pyfltr.cli.main
import pyfltr.command.execution_slot
import pyfltr.command.stage_runner
from tests.conftest import write_slot_wait_worker
from tests.integration import concurrent_runs_helper as helper

_API_HOLDER_SCRIPT = """import os
import pathlib
import sys
import time

import pyfltr.command.execution_slot

log, release = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
with pyfltr.command.execution_slot.hold(label="external-check"):
    print(os.getpid(), flush=True)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"x start {time.time()}\\n")
    deadline = time.monotonic() + 120
    while not release.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(release)
        time.sleep(0.02)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"x end {time.time()}\\n")
"""


def _assert_after(log: pathlib.Path, first: str, second: str) -> None:
    """`second`の区間が`first`の区間の終了後に始まったことを確かめる。"""
    spans = helper.intervals(log)
    first_end = spans[first][1]
    assert first_end is not None, spans
    assert spans[second][0] >= first_end, spans


@pytest.mark.timeout(240)
@pytest.mark.parametrize("subcommand", ["run", "run-for-agent", "fast", "ci"])
def test_cli_runs_do_not_overlap(tmp_path: pathlib.Path, subcommand: str) -> None:
    """別cwd・別の起動形の2つのCLIは、後発が待機を通知し、先発の終了後にツールを実行して完了する。"""
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-a"
    project_a = helper.make_project(tmp_path, "a", log=log, release=release)
    project_b = helper.make_project(tmp_path, "b", log=log, release=None)

    with helper.start(helper.pyfltr_argv("run"), project_a) as run_a:
        helper.wait_for(
            lambda: helper.started(log, "a"), expected="Aがツールの区間を開始する", observed=lambda: helper.describe(log, run_a)
        )
        with helper.start(helper.pyfltr_argv(subcommand, output_format="text", script=True), project_b) as run_b:
            helper.wait_for(
                lambda: helper.waiting_notified(run_b),
                expected="Bが実行枠の待機を通知する",
                observed=lambda: helper.describe(log, run_a, run_b),
            )
            assert not helper.started(log, "b")
            release.touch()
            assert run_a.wait() == 0, run_a.stderr
            assert run_b.wait() == 0, run_b.stderr
    _assert_after(log, "a", "b")
    assert f"cwd={project_a}" in run_b.stderr


@pytest.mark.timeout(240)
def test_mcp_run_waits_for_cli(tmp_path: pathlib.Path) -> None:
    """MCPの`run`はCLIの実行が終わってからツールを実行し、待機を応答の警告で返す。"""
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-a"
    project_a = helper.make_project(tmp_path, "a", log=log, release=release)
    project_b = helper.make_project(tmp_path, "b", log=log, release=None)
    request = tmp_path / "mcp-request.json"
    request.write_text(
        json.dumps({"paths": ["sample.txt"], "commands": [helper.PROBE_COMMAND], "work_dir": str(project_b), "no_cache": True}),
        encoding="utf-8",
    )
    waiting = tmp_path / "slot-waiting"
    worker_script = write_slot_wait_worker(tmp_path / "worker.py", waiting)

    with helper.start(helper.pyfltr_argv("run"), project_a) as run_a:
        helper.wait_for(
            lambda: helper.started(log, "a"), expected="Aがツールの区間を開始する", observed=lambda: helper.describe(log, run_a)
        )
        with helper.start([sys.executable, "-I", str(worker_script), "tool_run"], project_b, stdin_path=request) as worker:
            # MCPは警告を応答へまとめて返すため、workerが実行枠の待機を始めた通知を同期点にする。
            helper.wait_for(
                waiting.exists,
                expected="MCP workerが実行枠の待機を始める",
                observed=lambda: helper.describe(log, run_a, worker),
            )
            assert not helper.started(log, "b")
            release.touch()
            assert run_a.wait() == 0, run_a.stderr
            assert worker.wait() == 0, worker.stderr
    response = json.loads(worker.stdout)["result"]
    assert response["exit_code"] == 0
    assert [warning["source"] for warning in response["warnings"]] == ["execution-slot"]
    _assert_after(log, "a", "b")


@pytest.mark.timeout(240)
def test_nested_children_inherit_slot(tmp_path: pathlib.Path) -> None:
    """検証コマンドが起動した子のpyfltrとAPI呼出は待たずに完了し、独立した実行は親の終了まで始まらない。"""
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-p"
    project_c = helper.make_project(tmp_path, "c", log=log, release=None)
    project_d = helper.make_project(tmp_path, "d", log=log, release=None)
    nested = tmp_path / "nested.py"
    nested.write_text(
        textwrap.dedent(
            """
            import pathlib
            import subprocess
            import sys
            import time

            log, child_project, release = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
            with log.open("a", encoding="utf-8") as f:
                f.write(f"p start {time.time()}\\n")
            child = subprocess.run(
                [sys.executable, "-m", "pyfltr", "run", "--commands=slot-probe", "--output-format=jsonl", "."],
                cwd=child_project,
                timeout=120,
                check=False,
            )
            # 祖先が保持中なら継承して即座に入る。継承できなければ中断判定でWaitAbortedになり非0で終わる。
            api = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import pyfltr.command.execution_slot as s\\nwith s.hold(should_abort=lambda: True): print('api ok')",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            print(f"child={child.returncode} api={api.returncode} {api.stdout.strip()}", flush=True)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"n start {time.time()}\\n")
            deadline = time.monotonic() + 120
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(release)
                time.sleep(0.02)
            with log.open("a", encoding="utf-8") as f:
                f.write(f"p end {time.time()}\\n")
            sys.exit(0 if child.returncode == 0 and api.returncode == 0 else 1)
            """
        ),
        encoding="utf-8",
    )
    nested_command = {
        "nested": {
            "type": "tester",
            "path": sys.executable,
            "args": [str(nested), str(helper.record_path(log, "p")), str(project_c), str(release)],
            "targets": "*.txt",
            "pass-filenames": False,
        }
    }
    project_p = helper.make_project(tmp_path, "p", log=log, release=None, extra_commands=nested_command)

    with helper.start(helper.pyfltr_argv("run", commands="nested"), project_p) as run_p:
        helper.wait_for(
            lambda: "n" in helper.intervals(log),
            expected="親の検証コマンドが子の実行を終える",
            observed=lambda: helper.describe(log, run_p),
        )
        with helper.start(helper.pyfltr_argv("run", output_format="text"), project_d) as run_d:
            helper.wait_for(
                lambda: helper.waiting_notified(run_d),
                expected="Dが実行枠の待機を通知する",
                observed=lambda: helper.describe(log, run_p, run_d),
            )
            assert not helper.started(log, "d")
            release.touch()
            assert run_p.wait() == 0, run_p.stdout
            assert run_d.wait() == 0, run_d.stderr
    spans = helper.intervals(log)
    p_end = spans["p"][1]
    assert p_end is not None
    assert spans["p"][0] <= spans["c"][0] and spans["c"][1] is not None and spans["c"][1] <= p_end, spans
    _assert_after(log, "p", "d")


@pytest.mark.skipif(os.name == "nt", reason="待機中のプロセスへSIGINTを送る経路はPOSIXで検証する")
@pytest.mark.timeout(240)
def test_interrupt_while_waiting(tmp_path: pathlib.Path) -> None:
    """待機中に中断したCLIはツールを実行せず非0で終わり、成功の結果を出力しない。"""
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-a"
    project_a = helper.make_project(tmp_path, "a", log=log, release=release)
    project_b = helper.make_project(tmp_path, "b", log=log, release=None)

    with helper.start(helper.pyfltr_argv("run"), project_a) as run_a:
        helper.wait_for(
            lambda: helper.started(log, "a"), expected="Aがツールの区間を開始する", observed=lambda: helper.describe(log, run_a)
        )
        with helper.start(helper.pyfltr_argv("run", output_format="text"), project_b) as run_b:
            helper.wait_for(
                lambda: helper.waiting_notified(run_b),
                expected="Bが実行枠の待機を通知する",
                observed=lambda: helper.describe(log, run_a, run_b),
            )
            run_b.process.send_signal(signal.SIGINT)
            assert run_b.wait() != 0
        release.touch()
        assert run_a.wait() == 0, run_a.stderr
    assert not helper.started(log, "b")


@pytest.mark.timeout(240)
@pytest.mark.parametrize(("exit_code", "timeout"), [pytest.param(1, None, id="failed"), pytest.param(0, 8, id="timeout")])
def test_slot_released_after_failure_and_timeout(tmp_path: pathlib.Path, exit_code: int, timeout: int | None) -> None:
    """ツールの失敗・timeoutで終わった実行は子の停止後に枠を解放し、待っていた実行が進む。"""
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-a"
    extra = {"command-timeout": timeout} if timeout is not None else {}
    project_a = helper.make_project(tmp_path, "a", log=log, release=release, exit_code=exit_code, extra_config=extra)
    project_b = helper.make_project(tmp_path, "b", log=log, release=None)

    with helper.start(helper.pyfltr_argv("run"), project_a) as run_a:
        helper.wait_for(
            lambda: helper.started(log, "a"), expected="Aがツールの区間を開始する", observed=lambda: helper.describe(log, run_a)
        )
        with helper.start(helper.pyfltr_argv("run", output_format="text"), project_b) as run_b:
            helper.wait_for(
                lambda: helper.waiting_notified(run_b),
                expected="Bが実行枠の待機を通知する",
                observed=lambda: helper.describe(log, run_a, run_b),
            )
            if timeout is None:
                release.touch()
            assert run_a.wait() == 1, run_a.stderr
            assert run_b.wait() == 0, run_b.stderr
    release.touch()
    spans = helper.intervals(log)
    last_alive = spans["a"][1] if timeout is None else helper.last_tick(log, "a")
    assert last_alive is not None, spans
    assert spans["b"][0] >= last_alive, spans


@pytest.mark.timeout(240)
def test_api_holder_excludes_pyfltr_run(tmp_path: pathlib.Path) -> None:
    """APIで枠を保持する別プロジェクト相当の検証と、pyfltrの実行は区間を共有しない。"""
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-x"
    holder_dir = tmp_path / "x"
    holder_dir.mkdir()
    script = holder_dir / "holder.py"
    script.write_text(_API_HOLDER_SCRIPT, encoding="utf-8")
    project_b = helper.make_project(tmp_path, "b", log=log, release=None)

    with helper.start([sys.executable, str(script), str(helper.record_path(log, "x")), str(release)], holder_dir) as holder:
        helper.wait_for(
            lambda: helper.started(log, "x"),
            expected="APIの保持者が区間を開始する",
            observed=lambda: helper.describe(log, holder),
        )
        with helper.start(helper.pyfltr_argv("run", output_format="text"), project_b) as run_b:
            helper.wait_for(
                lambda: helper.waiting_notified(run_b),
                expected="Bが実行枠の待機を通知する",
                observed=lambda: helper.describe(log, holder, run_b),
            )
            assert not helper.started(log, "b")
            release.touch()
            assert holder.wait() == 0, holder.stderr
            assert run_b.wait() == 0, run_b.stderr
    # Windowsの仮想環境の`python.exe`は本体を子プロセスで起動するため、保持者が自ら報告したpidと比べる。
    assert f"pid={holder.stdout.strip()}" in run_b.stderr
    _assert_after(log, "x", "b")


def _show_run_timing(run_id: str, *, cwd: pathlib.Path) -> dict:
    """公開の`show-run`をjson形式で別プロセス起動し、`timing`を返す。"""
    completed = subprocess.run(
        [sys.executable, "-m", "pyfltr", "show-run", run_id, "--output-format=json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    return {**payload["timing"], "commands": payload["commands"]}


def _iso(timestamp: float) -> str:
    return dt.datetime.fromtimestamp(timestamp, dt.UTC).isoformat()


@pytest.mark.timeout(240)
def test_cli_records_slot_wait_and_body(tmp_path: pathlib.Path) -> None:
    """待たされた実行は、待機区間・本体区間・run全体の経過を`show-run`から区別して取得できる。

    先発は待たずに取得した対照とし、後発の待機区間だけが先発の保持時間を含む。
    """
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-a"
    project_a = helper.make_project(tmp_path, "a", log=log, release=release)
    project_b = helper.make_project(tmp_path, "b", log=log, release=None)

    with helper.start(helper.pyfltr_argv("run"), project_a) as run_a:
        helper.wait_for(
            lambda: helper.started(log, "a"), expected="Aがツールの区間を開始する", observed=lambda: helper.describe(log, run_a)
        )
        with helper.start(helper.pyfltr_argv("run"), project_b) as run_b:
            helper.wait_for(
                lambda: any(record.get("kind") == "header" for record in run_b.records()),
                expected="Bが実行を開始する",
                observed=lambda: helper.describe(log, run_a, run_b),
            )
            time.sleep(1.0)
            assert not helper.started(log, "b")
            release.touch()
            assert run_a.wait() == 0, run_a.stderr
            assert run_b.wait() == 0, run_b.stderr

    helper.intervals(log)
    control = _show_run_timing(helper.header_run_id(run_a), cwd=project_a)
    waited = _show_run_timing(helper.header_run_id(run_b), cwd=project_b)
    for timing in (control, waited):
        assert timing["measured"], timing
        assert timing["unfinished_slot_waits"] == 0, timing
        assert [(wait["kind"], wait["sequence"], wait["outcome"]) for wait in timing["slot_waits"]] == [
            ("initial", 0, "acquired")
        ], timing
    waited_wait = waited["slot_waits"][0]
    assert waited_wait["elapsed"] >= 1.0, waited
    assert control["slot_wait_total"] < waited["slot_wait_total"], (control, waited)
    # 本体区間は待機区間が閉じた後に始まる。
    body = waited["commands"][0]
    assert body["started_at"] >= waited_wait["finished_at"], waited
    assert body["finished_at"] >= body["started_at"], waited
    assert waited["run_elapsed"] >= waited["slot_wait_total"] + body["elapsed"], waited


@pytest.mark.timeout(120)
def test_cli_reports_slot_wait_progress_and_resume(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """別の保持者を待つCLIは、待機の継続（保持者・見込みを示せない理由）と再開を逐次出力して完了する。"""
    project = helper.make_project(tmp_path, "a", log=tmp_path / "intervals.log", release=None)
    monkeypatch.setattr(pyfltr.command.stage_runner, "_SLOT_PROGRESS_DELAY", 0.2)
    monkeypatch.setattr(pyfltr.command.stage_runner, "_SLOT_PROGRESS_INTERVAL", 0.2)
    holding = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with pyfltr.command.execution_slot.hold(label="external-check"):
            holding.set()
            release.wait(60)

    thread = threading.Thread(target=_holder)
    thread.start()
    timer = threading.Timer(1.5, release.set)
    try:
        assert holding.wait(30)
        timer.start()
        returncode = pyfltr.cli.main.run(
            ["run", f"--commands={helper.PROBE_COMMAND}", "--output-format=text", "--work-dir", str(project), str(project)]
        )
    finally:
        release.set()
        timer.cancel()
        thread.join(30)
    out = capsys.readouterr().out
    assert returncode == 0
    assert "実行枠の取得を待っています" in out
    assert "external-check" in out
    assert "残り時間の見込みは" in out
    assert "実行枠を取得しました" in out


@pytest.mark.timeout(120)
def test_cli_without_candidate_targets_does_not_wait(tmp_path: pathlib.Path) -> None:
    """対象を持ち得ないツールだけの実行は、別の保持者がいても実行枠を待たずに完了し、待機区間を記録しない。

    文書だけをcommitしたときのコミットフックのように、対象の無いツールが実行枠の取得・取り直しを待つと、
    外部ツールを起動しないまま長時間待つ。
    """
    project = helper.make_project(tmp_path, "a", log=tmp_path / "intervals.log", release=None)
    readme = project / "README.md"
    readme.write_text("# a\n", encoding="utf-8")
    holder_release = tmp_path / "release-holder"
    holder_script = tmp_path / "holder.py"
    holder_script.write_text(_API_HOLDER_SCRIPT, encoding="utf-8")
    with subprocess.Popen(
        [sys.executable, str(holder_script), str(tmp_path / "holder.log"), str(holder_release)],
        stdout=subprocess.PIPE,
        text=True,
    ) as holder:
        try:
            assert holder.stdout is not None
            holder.stdout.readline()
            completed = subprocess.run(
                [*helper.pyfltr_argv("fast", output_format="jsonl")[:-1], str(readme)],
                cwd=project,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        finally:
            holder_release.touch()
            holder.wait(30)
    assert completed.returncode == 0, completed.stderr
    records = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    commands = [record for record in records if record["kind"] == "command"]
    assert [(record["command"], record["status"]) for record in commands] == [(helper.PROBE_COMMAND, "skipped")]
    run_id = next(str(record["run_id"]) for record in records if record["kind"] == "header")
    timing = _show_run_timing(run_id, cwd=project)
    assert timing["slot_waits"] == [], timing
