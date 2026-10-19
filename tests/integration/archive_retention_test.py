"""実行中のrunを別runの保持cleanupから守り、終了後に参照できることの結合試験。

run Aを別プロセスのCLIで実行したまま、同じ保存先で別のrun Bを起動する。
Bは実行アーカイブの初期化（cleanupを含む）を終えてから実行枠を待つため、待機の通知を観測した時点で
Bのcleanupは完了している。保持数1の設定では、保護が無ければ古いAが削除される。
"""

import pathlib
import subprocess
import sys

import psutil
import pytest

from tests.integration import concurrent_runs_helper as helper


def _show_run_output(run_id: str, command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pyfltr", "show-run", run_id, "--commands", command, "--output", "--output-format=text"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )


@pytest.mark.timeout(240)
@pytest.mark.parametrize(
    ("exit_code", "timeout", "expected_returncode"),
    [
        pytest.param(0, None, 0, id="succeeded"),
        pytest.param(1, None, 1, id="failed"),
        pytest.param(0, 8, 1, id="timeout"),
    ],
)
def test_running_run_survives_other_cleanup(
    tmp_path: pathlib.Path, exit_code: int, timeout: int | None, expected_returncode: int
) -> None:
    """Aの実行中に別runのcleanupが起きても、Aの記録が残り終了後に公開CLIで取得できる。

    timeoutの場合はAのツールを解放せず、pyfltrがツールの時間上限で停止する。
    """
    log = tmp_path / "intervals.log"
    release = tmp_path / "release-a"
    extra = {"archive-max-runs": 1, **({"command-timeout": timeout} if timeout is not None else {})}
    project_a = helper.make_project(tmp_path, "a", log=log, release=release, exit_code=exit_code, extra_config=extra)
    project_b = helper.make_project(tmp_path, "b", log=log, release=None, extra_config={"archive-max-runs": 1})

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
            a_returncode = run_a.wait()
            assert run_b.wait() == 0, run_b.stderr

    assert a_returncode == expected_returncode
    run_id = helper.header_run_id(run_a)
    assert (helper.cache_root() / "runs" / run_id / "meta.json").exists()
    shown = _show_run_output(run_id, helper.PROBE_COMMAND)
    assert shown.returncode == 0, shown.stderr
    assert "probe output a" in shown.stdout


@pytest.mark.timeout(240)
def test_killed_run_is_readable_then_collected(tmp_path: pathlib.Path) -> None:
    """強制終了したrunは保存済みの結果を参照でき、次のrunの保持上限で回収される。"""
    log = tmp_path / "intervals.log"
    quick = tmp_path / "quick.py"
    quick.write_text("print('quick done')\n", encoding="utf-8")
    extra_commands = {
        "quick-check": {
            "type": "linter",
            "path": sys.executable,
            "args": [str(quick)],
            "targets": "*.txt",
            "pass-filenames": False,
        }
    }
    project_a = helper.make_project(
        tmp_path,
        "a",
        log=log,
        release=tmp_path / "never-released",
        extra_config={"archive-max-runs": 1, "jobs": 2},
        extra_commands=extra_commands,
    )
    project_b = helper.make_project(tmp_path, "b", log=log, release=None, extra_config={"archive-max-runs": 1})

    runs_dir = helper.cache_root() / "runs"
    with helper.start(helper.pyfltr_argv("run", commands=f"quick-check,{helper.PROBE_COMMAND}"), project_a) as run_a:
        helper.wait_for(
            lambda: helper.started(log, "a"), expected="Aがツールの区間を開始する", observed=lambda: helper.describe(log, run_a)
        )
        helper.wait_for(
            lambda: any(runs_dir.glob("*/tools/quick-check/tool.json")),
            expected="Aがquick-checkの結果を保存する",
            observed=lambda: helper.describe(log, run_a),
        )
        run_id = next(runs_dir.glob("*/tools/quick-check/tool.json")).parents[2].name
        root = psutil.Process(run_a.process.pid)
        processes = [root, *root.children(recursive=True)]
        for process in processes:
            process.kill()
        psutil.wait_procs(processes, timeout=30)

    shown = _show_run_output(run_id, "quick-check")
    assert shown.returncode == 0, shown.stderr
    assert "quick done" in shown.stdout

    with helper.start(helper.pyfltr_argv("run"), project_b) as run_b:
        assert run_b.wait() == 0, run_b.stderr
    assert not (runs_dir / run_id).exists()
    assert not (runs_dir / f"{run_id}.lock").exists()
