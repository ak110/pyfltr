import collections.abc
import contextlib
import os
import pathlib
import subprocess
import sys
import textwrap
import time
import typing

import psutil
import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_

_SLEEP_BEYOND_TEST_TIMEOUT = "import time; time.sleep(300)"


def test_run_subprocess_file_not_found_returns_127() -> None:
    result = pyfltr.command.process.run_subprocess(
        ["this-command-definitely-does-not-exist-xyz-1234"],
        env={"PATH": "/nonexistent"},
    )
    assert result.returncode == 127
    assert "見つかりません" in result.stdout


class _FakePopen:
    last_args_holder: list[list[str]] = []

    def __init__(self, args: list[str], **kwargs: typing.Any) -> None:
        del kwargs  # Popen互換の追加引数を受け取るのみ
        _FakePopen.last_args_holder.append(list(args))
        self.returncode = 0
        self.stdout: typing.Iterator[str] = iter([])

    def __enter__(self) -> "_FakePopen":
        return self

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        del exc_type, exc, tb  # contextmanager互換の引数を受け取るのみ

    def wait(self) -> int:
        return self.returncode


@pytest.mark.parametrize("command", ["pre-commit", "prek"])
def test_run_subprocess_resolves_command_via_shutil_which(mocker, command: str) -> None:
    _FakePopen.last_args_holder = []
    resolved = f"/resolved/{command}"
    mocker.patch("pyfltr.command.process.shutil.which", return_value=resolved)
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess([command, "run", "--files", "foo.py"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [[resolved, "run", "--files", "foo.py"]]


def test_run_subprocess_keeps_original_name_when_unresolved(mocker) -> None:
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.shutil.which", return_value=None)
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess(["missing-tool", "arg"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [["missing-tool", "arg"]]


def test_run_subprocess_resolves_via_env_path(mocker, tmp_path: pathlib.Path, monkeypatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # WindowsのshUtil.whichはPATHEXTに列挙された拡張子で実行ファイルを判定するため、
    # ダミー実行ファイル名を`.bat`にする（POSIXでは実行属性0o755で判定される）。
    if os.name == "nt":
        target = bin_dir / "faketool.bat"
        target.write_text("")
    else:
        target = bin_dir / "faketool"
        target.write_text("")
        target.chmod(0o755)

    monkeypatch.setenv("PATH", "/nonexistent-pyfltr-test-path")

    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess(["faketool"], {"PATH": str(bin_dir)})

    assert len(_FakePopen.last_args_holder) == 1
    resolved = pathlib.Path(_FakePopen.last_args_holder[0][0])
    assert resolved.name.startswith("faketool")
    assert resolved.parent == bin_dir


def test_run_subprocess_does_not_mutate_commandline(mocker) -> None:
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.shutil.which", return_value="/resolved/tool")
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    original = ["tool", "arg"]
    pyfltr.command.process.run_subprocess(original, {"PATH": "/usr/bin"})

    assert original == ["tool", "arg"]


@pytest.mark.parametrize("command", ["faketool", "other-tool"])
def test_resolve_commandline_uses_env_path_without_mutation(tmp_path: pathlib.Path, monkeypatch, command: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if os.name == "nt":
        target = bin_dir / f"{command}.cmd"
        target.write_text("")
    else:
        target = bin_dir / command
        target.write_text("")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent-pyfltr-test-path")
    original = [command, "config", "--json"]

    resolved = pyfltr.command.process.resolve_commandline(original, {"PATH": str(bin_dir)})

    assert pathlib.Path(resolved[0]).parent == bin_dir
    assert pathlib.Path(resolved[0]).stem == command
    assert resolved[1:] == ["config", "--json"]
    assert original == [command, "config", "--json"]
    assert pyfltr.command.process.resolve_commandline(original, {"PATH": str(tmp_path / "empty")}) == original


@contextlib.contextmanager
def _registered_parent_with_child(script: str) -> collections.abc.Generator[tuple[subprocess.Popen[str], int, int], None, None]:
    with subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    ) as proc:
        try:
            assert proc.stdout is not None
            parent_pid_str, child_pid_str = proc.stdout.readline().strip().split()
            with pyfltr.command.process.active_processes_lock:
                pyfltr.command.process.active_processes.append(proc)
            yield proc, int(parent_pid_str), int(child_pid_str)
        finally:
            with pyfltr.command.process.active_processes_lock:
                if proc in pyfltr.command.process.active_processes:
                    pyfltr.command.process.active_processes.remove(proc)
            # POSIX限定の経路（呼出元の試験はWindowsでskipする）。型チェッカーの誤検知は局所コメントで抑止する。
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(proc.pid, 9)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member


def _wait_gone(pids: list[int], *, timeout: float) -> list[int]:

    def _is_alive(pid: int) -> bool:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if _is_alive(pid)]
        if not alive:
            return []
        time.sleep(0.05)
    return [pid for pid in pids if _is_alive(pid)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 前提の killpg 経路を検証する")
def test_terminate_active_processes_kills_grandchild() -> None:
    script = textwrap.dedent(
        """
        import os, time
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            # child: pidをpipeへ書き、あとは待機する（stdoutへは書かない）。
            os.close(r)
            os.write(w, str(os.getpid()).encode())
            os.close(w)
            while True:
                time.sleep(1)
        else:
            # 親: childのpidを読み取り、自身とchildのpidを1行にまとめて出力する。
            os.close(w)
            child_pid = int(os.read(r, 64).decode())
            os.close(r)
            print(f"{os.getpid()} {child_pid}", flush=True)
            while True:
                time.sleep(1)
        """
    )
    with _registered_parent_with_child(script) as (_proc, parent_pid, child_pid):
        assert psutil.pid_exists(parent_pid)
        assert psutil.pid_exists(child_pid)

        pyfltr.command.process.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([parent_pid, child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかったpid: {remaining}"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 前提の killpg 経路を検証する")
def test_terminate_active_processes_parent_exited_grandchild_remains() -> None:
    script = textwrap.dedent(
        """
        import os, time
        pid = os.fork()
        if pid == 0:
            # grandchild役。stdoutを継承したまま待機する。
            while True:
                time.sleep(1)
        else:
            # 親だけがstdoutに出力してすぐexit。grandchildはstdoutを保持し続ける。
            print(f"{os.getpid()} {pid}", flush=True)
            os._exit(0)
        """
    )
    with _registered_parent_with_child(script) as (proc, _parent_pid, child_pid):
        proc.wait(timeout=2.0)
        assert psutil.pid_exists(child_pid), "孫プロセスが消えている"

        pyfltr.command.process.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかったpid: {remaining}"


def _make_env() -> dict[str, str]:
    return dict(os.environ)


def test_run_subprocess_no_timeout_returns_completed_normally() -> None:
    proc = pyfltr.command.process.run_subprocess(
        [sys.executable, "-c", "print('ok')"],
        _make_env(),
    )
    assert isinstance(proc, subprocess.CompletedProcess)
    assert proc.returncode == 0
    assert "ok" in proc.stdout


def test_run_subprocess_raises_timeout_when_exceeded() -> None:
    with pytest.raises(pyfltr.command.process.TimeoutExceededExecution) as exc_info:
        pyfltr.command.process.run_subprocess(
            [sys.executable, "-c", _SLEEP_BEYOND_TEST_TIMEOUT],
            _make_env(),
            timeout=0.1,
        )
    assert exc_info.value.timeout == 0.1
    assert exc_info.value.elapsed >= 0.1
    assert isinstance(exc_info.value.output, str)


def test_run_process_loop_wrapper_returns_failed_completed_process() -> None:
    proc = pyfltr.command.process.run_process_loop(
        [sys.executable, "-c", _SLEEP_BEYOND_TEST_TIMEOUT],
        _make_env(),
        timeout=0.1,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.returncode == 124
    assert proc.timeout_exceeded is True
    assert "Timeout exceeded after 0.1s" in proc.stdout
    assert "command-timeout" in proc.stdout


def test_run_process_loop_wrapper_passthrough_normal() -> None:
    proc = pyfltr.command.process.run_process_loop(
        [sys.executable, "-c", "print('ok')"],
        _make_env(),
        timeout=5.0,
    )
    assert proc.returncode == 0
    assert proc.timeout_exceeded is False
    assert "ok" in proc.stdout


def test_run_subprocess_interrupt_takes_priority_over_timeout() -> None:
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
    assert pyfltr.command.process.is_oom_returncode(returncode) == expected


def _make_fake_run_subprocess(returncodes: list[int]) -> typing.Callable[..., subprocess.CompletedProcess[str]]:
    remaining = list(returncodes)

    def fake(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        rc = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        return subprocess.CompletedProcess(args=commandline, returncode=rc, stdout="")

    return fake


def test_run_process_loop_retries_on_oom_and_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == 0
    assert proc.retry_count == 1
    assert proc.timeout_exceeded is False


def test_run_process_loop_no_retry_when_retry_on_oom_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=False,
        retry_max_attempts=1,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 0


def test_run_process_loop_no_retry_when_max_attempts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=0,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 0


def test_run_process_loop_retries_up_to_max_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([137, 137, 137]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=2,
    )
    assert proc.returncode == 137
    assert proc.retry_count == 2


def test_run_process_loop_no_retry_on_non_oom_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([1, 0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == 1
    assert proc.retry_count == 0


def test_run_process_loop_no_retry_count_on_normal_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pyfltr.command.process,
        "run_subprocess",
        _make_fake_run_subprocess([0]),
    )
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
    )
    assert proc.returncode == 0
    assert proc.retry_count == 0


def test_run_process_loop_no_retry_when_timeout_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:

    def fake_timeout(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        del commandline
        raise pyfltr.command.process.TimeoutExceededExecution(output="", elapsed=2.0, timeout=1.0)

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess", fake_timeout)
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=3,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.timeout_exceeded is True
    assert proc.retry_count == 0


def test_run_process_loop_oom_then_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
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
    proc = pyfltr.command.process.run_process_loop(
        ["dummy"],
        _make_env(),
        retry_on_oom=True,
        retry_max_attempts=1,
    )
    assert proc.returncode == pyfltr.command.process.TIMEOUT_RETURNCODE
    assert proc.timeout_exceeded is True
    assert proc.retry_count == 1


def test_timeout_reports_remaining_processes_with_action(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:

    class _RemainingProcess:
        pid = 424242

        def kill(self) -> None:
            pass

    def _wait_procs(procs: typing.Any, timeout: float | None = None) -> tuple[list[typing.Any], list[typing.Any]]:
        del procs, timeout
        time.sleep(0.5)
        return [], [_RemainingProcess()]

    monkeypatch.setattr(pyfltr.command.process.psutil, "wait_procs", _wait_procs)
    with caplog.at_level("WARNING", logger="pyfltr.command.process"):
        proc = pyfltr.command.process.run_process_loop(
            [sys.executable, "-c", _SLEEP_BEYOND_TEST_TIMEOUT],
            _make_env(),
            timeout=0.1,
        )
    assert proc.timeout_exceeded is True
    assert "424242" in caplog.text
    assert "Get-Process -Id <PID>" in caplog.text
    assert "kill -KILL <PID>" in caplog.text
