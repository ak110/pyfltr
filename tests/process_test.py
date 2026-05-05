"""`pyfltr.command.process.run_subprocess` の timeout 関連テスト。

軽量な Python サブプロセスを起動し、timeout 発火と `is_interrupted` との整合性を検証する。
タイムアウト値は 0.1 秒等の小さな固定値で 1 秒以内に収まる構成にする。
"""

import os
import subprocess
import sys

import pytest

import pyfltr.command.process


def _make_env() -> dict[str, str]:
    """run_subprocess に渡す最小 env。PATH 等を引き継いで Python インタープリターを解決可能にする。"""
    return dict(os.environ)


def test_run_subprocess_no_timeout_returns_completed_normally() -> None:
    """timeout=Noneでは即座に終了するsubprocessが正常完了することを確認する。"""
    proc = pyfltr.command.process.run_subprocess(
        [sys.executable, "-c", "print('ok')"],
        _make_env(),
    )
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert "ok" in proc.stdout


def test_run_subprocess_raises_timeout_when_exceeded() -> None:
    """timeout超過時に `TimeoutExceededExecution` を送出することを確認する。

    `time.sleep(2)` するsubprocessを0.1秒のtimeoutで起動し、Timer発火で停止 → 例外送出に至る経路を検証する。
    """
    with pytest.raises(pyfltr.command.process.TimeoutExceededExecution) as exc_info:
        pyfltr.command.process.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            _make_env(),
            timeout=0.1,
        )
    # 例外オブジェクトが途中までのoutputとelapsed情報を保持する。
    assert exc_info.value.timeout == 0.1
    assert exc_info.value.elapsed >= 0.1
    # outputはsubprocessが何も出力していないため空文字列。
    assert isinstance(exc_info.value.output, str)


def test_run_subprocess_with_timeout_wrapper_returns_failed_completed_process() -> None:
    """`run_subprocess_with_timeout` がtimeout時に `returncode=124` の `CompletedProcess` を返すことを確認する。"""
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        [sys.executable, "-c", "import time; time.sleep(2)"],
        _make_env(),
        timeout=0.1,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.returncode == 124
    assert proc.timeout_exceeded is True
    assert "Timeout exceeded after 0.1s" in proc.stdout


def test_run_subprocess_with_timeout_wrapper_passthrough_normal() -> None:
    """`run_subprocess_with_timeout` は通常終了時に `timeout_exceeded=False` を保持する。"""
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        [sys.executable, "-c", "print('ok')"],
        _make_env(),
        timeout=5.0,
    )
    assert proc.returncode == 0
    assert proc.timeout_exceeded is False
    assert "ok" in proc.stdout


def test_run_subprocess_interrupt_takes_priority_over_timeout() -> None:
    """`is_interrupted=True` 時はtimeout監視より先に `InterruptedExecution` が送出される。

    `is_interrupted` は `Popen` 直前にチェックされるため、timeout発火を待たずに送出される。
    既存の中断系挙動とtimeout系挙動が共存できることを確認する。
    """
    with pytest.raises(pyfltr.command.process.InterruptedExecution):
        pyfltr.command.process.run_subprocess(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            _make_env(),
            is_interrupted=lambda: True,
            timeout=0.1,
        )
