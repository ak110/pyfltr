from __future__ import annotations

# psutilは、プロセスツリーの停止で初めて使う時に読み込む。
# Python 3.15以上で遅延importとなり、それ未満の版では通常のimportとして扱われる（PEP 810）。
__lazy_modules__ = ["psutil"]

import atexit
import contextlib
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import threading
import time
import typing

import psutil

import pyfltr.command.core_
import pyfltr.config.model
from pyfltr.command.env import get_env_path

logger = __import__("logging").getLogger(__name__)


class ProcessRegistry:
    def __init__(self) -> None:
        self.processes: list[subprocess.Popen[str]] = []
        self.lock = threading.Lock()

    def add(self, proc: subprocess.Popen[str]) -> None:
        with self.lock:
            self.processes.append(proc)

    def remove(self, proc: subprocess.Popen[str]) -> None:
        with self.lock, contextlib.suppress(ValueError):
            self.processes.remove(proc)

    def snapshot(self) -> list[subprocess.Popen[str]]:
        with self.lock:
            return list(self.processes)

    def terminate_all(self, *, timeout: float) -> None:
        for proc in self.snapshot():
            with contextlib.suppress(OSError):
                _kill_process_tree(proc, timeout=timeout)

    def cleanup(self) -> None:
        self.terminate_all(timeout=1.0)


_DEFAULT_REGISTRY = ProcessRegistry()

active_processes = _DEFAULT_REGISTRY.processes
active_processes_lock = _DEFAULT_REGISTRY.lock


class InterruptedExecution(Exception):
    pass


class TimeoutExceededExecution(Exception):
    def __init__(self, *, output: str, elapsed: float, timeout: float) -> None:
        super().__init__(f"timeout exceeded after {timeout:.1f}s (elapsed={elapsed:.1f}s)")
        self.output = output
        self.elapsed = elapsed
        self.timeout = timeout


def _kill_process_tree(proc: subprocess.Popen[str], *, timeout: float) -> None:
    targets: list[psutil.Process] = []
    if os.name == "nt":
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            parent = psutil.Process(proc.pid)
            targets = parent.children(recursive=True)
        with contextlib.suppress(OSError):
            proc.terminate()
        for child in targets:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.terminate()
    else:
        # os.killpg / os.getpgid / signal.SIGKILLはPOSIX専用でWindows型スタブに未定義。
        # os.nameガード下なので実行時は安全。型チェッカーの誤検知だけ局所コメントで抑止する。
        try:
            pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        except ProcessLookupError:
            pgid = proc.pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member

    wait_targets: list[psutil.Process] = list(targets)
    with contextlib.suppress(psutil.NoSuchProcess):
        wait_targets.append(psutil.Process(proc.pid))

    _, alive = psutil.wait_procs(wait_targets, timeout=timeout)

    if alive:
        if os.name == "nt":
            for child in alive:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    child.kill()
        else:
            try:
                pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
            except ProcessLookupError:
                pgid = proc.pid
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        _, still_alive = psutil.wait_procs(alive, timeout=timeout)
        if still_alive:
            remaining_pids = [p.pid for p in still_alive]
            logger.warning(
                "プロセスツリーの停止を試みましたが終了しないプロセスが残っています: pids=%s。"
                "表示したPIDをWindowsでは `Get-Process -Id <PID>`、POSIXでは `ps -p <PID> -o pid,args` で確認し、"
                "この実行のプロセスが残っている場合は `Stop-Process -Id <PID> -Force` または "
                "`kill -KILL <PID>` で停止してください",
                remaining_pids,
            )


atexit.register(_DEFAULT_REGISTRY.cleanup)


def terminate_active_processes(*, timeout: float = 5.0) -> None:
    _DEFAULT_REGISTRY.terminate_all(timeout=timeout)


def _terminate_and_drop(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(OSError):
        _kill_process_tree(proc, timeout=5.0)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5.0)
    _DEFAULT_REGISTRY.remove(proc)


def _on_timeout(proc: subprocess.Popen[str], fired: threading.Event) -> None:
    fired.set()
    with contextlib.suppress(OSError):
        _kill_process_tree(proc, timeout=5.0)


def resolve_commandline(commandline: list[str], env: dict[str, str]) -> list[str]:
    resolved = shutil.which(commandline[0], path=get_env_path(env))
    if resolved is not None and resolved != commandline[0]:
        return [resolved, *commandline[1:]]
    return commandline


def run_subprocess(
    commandline: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None = None,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
    timeout: float | None = None,
    cwd: pathlib.Path | None = None,
) -> subprocess.CompletedProcess[str]:
    popen_commandline = resolve_commandline(commandline, env)
    if is_interrupted is not None and is_interrupted():
        raise InterruptedExecution
    popen_extra: dict[str, typing.Any] = {}
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUPはWindows専用の定数。getattrの3引数形式を使うと
        # ruff B009のgetattr→属性アクセス変換対象外になるため、型チェッカー誤検知を回避できる。
        popen_extra["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_extra["start_new_session"] = True
    timeout_active = timeout is not None and timeout > 0
    timeout_fired = threading.Event()
    timer: threading.Timer | None = None
    start_monotonic = time.monotonic()
    try:
        with subprocess.Popen(
            popen_commandline,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            cwd=str(cwd) if cwd is not None else None,
            **popen_extra,
        ) as proc:
            _DEFAULT_REGISTRY.add(proc)
            subprocess_started = False

            try:
                if on_subprocess_start is not None:
                    on_subprocess_start()
                subprocess_started = True
                if is_interrupted is not None and is_interrupted():
                    _terminate_and_drop(proc)
                    raise InterruptedExecution

                if timeout_active:
                    assert timeout is not None
                    timer = threading.Timer(timeout, _on_timeout, args=(proc, timeout_fired))
                    timer.daemon = True
                    timer.start()

                output_lines: list[str] = []
                assert proc.stdout is not None
                for line in proc.stdout:
                    if is_interrupted is not None and is_interrupted():
                        _terminate_and_drop(proc)
                        raise InterruptedExecution
                    output_lines.append(line)
                    if on_output is not None:
                        on_output(line)
                proc.wait()
                if timeout_fired.is_set():
                    assert timeout is not None
                    assert timer is not None
                    timer.join()
                    raise TimeoutExceededExecution(
                        output="".join(output_lines),
                        elapsed=time.monotonic() - start_monotonic,
                        timeout=timeout,
                    )
                return subprocess.CompletedProcess(
                    args=commandline,
                    returncode=proc.returncode,
                    stdout="".join(output_lines),
                )
            finally:
                if timer is not None:
                    timer.cancel()
                if subprocess_started and on_subprocess_end is not None:
                    on_subprocess_end()
                _DEFAULT_REGISTRY.remove(proc)
    except FileNotFoundError as e:
        message = (
            f"実行ファイルが見つかりません: {commandline[0]} ({e})。PATHと対象ツールの `<command>-path` 設定を確認し、"
            "実行ファイルを導入するか存在するパスを指定してください。\n"
        )
        if on_output is not None:
            on_output(message)
        return subprocess.CompletedProcess(
            args=commandline,
            returncode=127,
            stdout=message,
        )


# POSIX慣習でtimeout停止に使われる124を踏襲し、シェル経由のtimeout(1)等と整合させる。
TIMEOUT_RETURNCODE: int = 124

OOM_RETURNCODES: frozenset[int] = frozenset({-9, 137})


def is_oom_returncode(returncode: int | None) -> bool:
    return returncode in OOM_RETURNCODES


class CompletedProcessWithTimeoutInfo(subprocess.CompletedProcess[str]):
    timeout_exceeded: bool
    retry_count: int

    def __init__(
        self,
        *,
        args: list[str] | tuple[str, ...],
        returncode: int,
        stdout: str,
        timeout_exceeded: bool = False,
        retry_count: int = 0,
    ) -> None:
        super().__init__(args=args, returncode=returncode, stdout=stdout)
        self.timeout_exceeded = timeout_exceeded
        self.retry_count = retry_count


def run_process_loop(
    commandline: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None = None,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
    timeout: float | None = None,
    cwd: pathlib.Path | None = None,
    retry_on_oom: bool = False,
    retry_max_attempts: int = 0,
    verbose: bool = False,
) -> CompletedProcessWithTimeoutInfo:
    if verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
    attempt = 0
    while True:
        try:
            proc = run_subprocess(
                commandline,
                env,
                on_output,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                timeout=timeout,
                cwd=cwd,
            )
        except TimeoutExceededExecution as e:
            message = (
                f"\nTimeout exceeded after {e.timeout:.1f}s (elapsed={e.elapsed:.1f}s)."
                " If the tool legitimately needs more time, raise `command-timeout`"
                " or the per-tool `<command>-timeout` in [tool.pyfltr].\n"
            )
            if on_output is not None:
                on_output(message)
            combined_output = e.output + message
            return CompletedProcessWithTimeoutInfo(
                args=commandline,
                returncode=TIMEOUT_RETURNCODE,
                stdout=combined_output,
                timeout_exceeded=True,
                retry_count=attempt,
            )
        if retry_on_oom and attempt < retry_max_attempts and is_oom_returncode(proc.returncode):
            attempt += 1
            logger.warning(
                "OOM検知のためリトライする。returncode=%s、試行 %d/%d。自動再試行中は対応不要です。"
                "最終的に失敗した場合はメモリーの空きを確保するか jobs を減らして再実行してください。",
                proc.returncode,
                attempt,
                retry_max_attempts,
            )
            continue
        return CompletedProcessWithTimeoutInfo(
            args=commandline,
            returncode=proc.returncode,
            stdout=proc.stdout,
            timeout_exceeded=False,
            retry_count=attempt,
        )


def run_process(
    request: pyfltr.command.core_.ExecutionRequest,
    commandline: list[str] | None = None,
) -> CompletedProcessWithTimeoutInfo:
    return run_process_loop(
        request.params.commandline if commandline is None else commandline,
        request.env,
        request.ctx.on_output,
        verbose=request.verbose,
        is_interrupted=request.ctx.is_interrupted,
        on_subprocess_start=request.ctx.on_subprocess_start,
        on_subprocess_end=request.ctx.on_subprocess_end,
        timeout=request.timeout,
        cwd=request.cwd,
        **request.retry_options,
    )
