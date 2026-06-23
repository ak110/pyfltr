"""`pyfltr.command.process.run_subprocess` の timeout 関連テスト。

軽量な Python サブプロセスを起動し、timeout 発火と `is_interrupted` との整合性を検証する。
タイムアウト値は 0.1 秒等の小さな固定値で 1 秒以内に収まる構成にする。
"""

import os
import subprocess
import sys
import typing

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


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [
        (-9, True),
        (137, True),
        (0, False),
        (1, False),
        (124, False),
        (None, False),
    ],
)
def test_is_oom_returncode(returncode: int | None, expected: bool) -> None:
    """`is_oom_returncode` がOOM該当returncodeのみTrueを返すことを確認する。"""
    assert pyfltr.command.process.is_oom_returncode(returncode) == expected


def _make_fake_run_subprocess(returncodes: list[int]) -> typing.Callable[..., subprocess.CompletedProcess[str]]:
    """試行ごとに指定のreturncodeを返すfake `run_subprocess`。

    `returncodes` の要素を順番に消費し、尽きたら末尾の値を繰り返す。
    """
    remaining = list(returncodes)

    def fake(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        rc = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return subprocess.CompletedProcess(args=commandline, returncode=rc, stdout="")

    return fake


def test_run_subprocess_with_timeout_retries_on_oom_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM returncode後に成功する場合、合計2回実行されて `retry_count == 1` となることを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == 0
    assert proc.retry_count == 1
    assert proc.timeout_exceeded is False


def test_run_subprocess_with_timeout_no_retry_when_retry_on_oom_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_on_oom=False` のときOOM returncodeでもリトライしないことを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
        retry_on_oom=False,
        retry_max_attempts=1,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 0


def test_run_subprocess_with_timeout_no_retry_when_max_attempts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_max_attempts=0` のときリトライしないことを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=0,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 0


def test_run_subprocess_with_timeout_retries_up_to_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    """`retry_max_attempts=2` で最大2回リトライ、3回OOMならfailed扱いになることを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 137, 137]),
    )
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=2,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 2


def test_run_subprocess_with_timeout_no_retry_on_non_oom_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """非OOM非ゼロreturncodeでリトライしないことを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([1, 0]),
    )
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == 1
    assert proc.retry_count == 0


def test_run_subprocess_with_timeout_no_retry_count_on_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """通常終了時に `retry_count == 0` であることを確認する。"""
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([0]),
    )
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
    )
    assert proc.returncode == 0
    assert proc.retry_count == 0


def test_run_subprocess_with_timeout_no_retry_when_timeout_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """timeout超過時はreturncodeがOOM相当値でもリトライしないことを確認する。

    `run_subprocess` が `TimeoutExceededExecution` を送出する経路はリトライ判定より前に
    `except` で早期returnするため、`retry_count` は0のままとなる。
    """

    def fake_timeout(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        del commandline
        raise pyfltr.command.process.TimeoutExceededExecution(output="", elapsed=2.0, timeout=1.0)

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess", fake_timeout)
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=3,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.timeout_exceeded is True
    assert proc.retry_count == 0


def test_run_subprocess_with_timeout_oom_then_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """OOM returncode後のリトライでtimeout超過が発生した場合の動作を確認する。

    1回目の `run_subprocess` でOOM returncode（137）を返し、
    2回目のリトライで `TimeoutExceededExecution` を送出するケースを検証する。
    期待値: `returncode == TIMEOUT_RETURNCODE`、`timeout_exceeded == True`、`retry_count == 1`。
    """
    call_count = 0

    def fake_oom_then_timeout(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> subprocess.CompletedProcess[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subprocess.CompletedProcess(args=commandline, returncode=137, stdout="")
        raise pyfltr.command.process.TimeoutExceededExecution(output="", elapsed=2.0, timeout=1.0)

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess", fake_oom_then_timeout)
    proc = pyfltr.command.process.run_subprocess_with_timeout(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.timeout_exceeded is True
    assert proc.retry_count == 1
