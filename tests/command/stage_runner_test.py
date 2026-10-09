import concurrent.futures
import pathlib
import threading

import pytest

import pyfltr.command.core_
import pyfltr.command.stage_runner
import pyfltr.config.config
import pyfltr.diagnostics
from tests.conftest import StageExecutorControl, make_command_result, make_execution_context


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
