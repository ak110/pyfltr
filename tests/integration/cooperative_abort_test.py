"""協調中断の実原因と既存のスキップ案内をステージ実行で保持する。"""

import pytest

import pyfltr.command.core_
import pyfltr.command.execution_slot
import pyfltr.command.stage_runner
import pyfltr.config.config
from tests.conftest import make_execution_context


@pytest.mark.parametrize(
    "cause",
    [
        "協調参加した子の処理が取り消されました。",
        "協調参加の登録が失われました。",
        "協調所有者が終了しました。",
        "協調区間が閉じられました。",
        "実行枠の取得待機が取り消されました。",
    ],
)
def test_checkpoint_abort_reason_reaches_skipped_result(monkeypatch: pytest.MonkeyPatch, cause: str) -> None:
    """外側の協調中断をfail-fastの案内へ置き換えない。"""
    reason = cause + "所有者と子を回収してから再実行してください。"

    def checkpoint() -> bool:
        raise pyfltr.command.execution_slot.WaitAborted(reason)

    monkeypatch.setattr(pyfltr.command.execution_slot, "yield_to_waiters", checkpoint)
    config = pyfltr.config.config.create_default_config()
    config.values.update({"ruff-format": True, "prettier": True})

    def execute(command: str, _fix: bool) -> pyfltr.command.core_.CommandResult:
        return pyfltr.command.core_.CommandResult(
            command=command,
            command_type="formatter",
            commandline=[],
            returncode=0,
            files=1,
            output="",
            elapsed=0,
        )

    results = pyfltr.command.stage_runner.run_stages(
        ["ruff-format", "prettier"],
        make_execution_context(config, []).base,
        execute,
    )
    assert [result.status for result in results] == ["succeeded", "skipped"]
    assert results[-1].returncode is None
    assert results[-1].output == reason


@pytest.mark.parametrize("interrupted", [False, True])
def test_explicit_interruption_and_fail_fast_keep_existing_guidance(interrupted: bool) -> None:
    """明示的な割り込みとfail-fastは既存の文面で未実行コマンドを案内する。"""
    config = pyfltr.config.config.create_default_config()
    config.values.update({"ruff-format": True, "prettier": True})

    def execute(command: str, _fix: bool) -> pyfltr.command.core_.CommandResult:
        return pyfltr.command.core_.CommandResult(
            command=command,
            command_type="formatter",
            commandline=[],
            returncode=2,
            formatter_failed=True,
            files=1,
            output="",
            elapsed=0,
        )

    results = pyfltr.command.stage_runner.run_stages(
        ["ruff-format", "prettier"],
        make_execution_context(config, []).base,
        execute,
        fail_fast=True,
        callbacks=pyfltr.command.stage_runner.StageCallbacks(is_interrupted=lambda: interrupted),
    )
    expected = (
        "Ctrl+C により中断しました。"
        if interrupted
        else "--fail-fast により実行をスキップしました。先に失敗したコマンドの指摘を修正して再実行してください。"
    )
    assert results[-1].status == "skipped"
    assert results[-1].output == expected
