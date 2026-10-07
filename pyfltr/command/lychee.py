"""lycheeの外部リンク一時障害の分類と有限再試行。"""

import pathlib
import time
import typing

import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
from pyfltr.link_checks import classify_failures


def run_lychee(
    commandline: list[str],
    config: pyfltr.config.model.Config,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    *,
    verbose: bool,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
    cwd: pathlib.Path | None = None,
) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
    """5xxを有限回再試行し、最後の実行だけを返す。

    lychee v0.24.2はRejectedStatusCodeの429だけを再試行するため5xxを補う。
    https://github.com/lycheeverse/lychee/blob/lychee-v0.24.2/lychee-lib/src/retry.rs
    応答タイムアウトの再試行はlychee自身へ委ねる。全試行と待機は同じ
    command-timeout予算を消費し、割込みも待機中に確認する。
    """
    timeout = pyfltr.config.model.resolve_command_timeout(config.values, "lychee")
    deadline = time.monotonic() + timeout if timeout is not None and timeout > 0 else None
    retry_count = 0
    retry_options = pyfltr.config.model.resolve_retry_kwargs(config.values)
    oom_limit = max(0, retry_options["retry_max_attempts"]) if retry_options["retry_on_oom"] else 0
    for attempt in range(4):
        # OOMの再試行でも時間予算を更新し、各試行へ元の上限を与え直さない。
        oom_attempt = 0
        while True:
            remaining = max(0.001, deadline - time.monotonic()) if deadline is not None else timeout
            proc = pyfltr.command.process.run_process_loop(
                commandline,
                env,
                on_output,
                verbose=verbose,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
                timeout=remaining,
                cwd=cwd,
            )
            if (
                proc.timeout_exceeded
                or not pyfltr.command.process.is_oom_returncode(proc.returncode)
                or oom_attempt == oom_limit
            ):
                break
            oom_attempt += 1
            retry_count += 1
        proc.retry_count = retry_count
        classification = classify_failures(proc.stdout)
        if proc.returncode != 2 or proc.timeout_exceeded or classification is None or not classification[1] or attempt == 3:
            return proc
        wait_until = time.monotonic() + 2**attempt
        if deadline is not None:
            wait_until = min(wait_until, deadline)
        while time.monotonic() < wait_until:
            if is_interrupted is not None and is_interrupted():
                raise pyfltr.command.process.InterruptedExecution
            time.sleep(min(0.1, max(0.0, wait_until - time.monotonic())))
        if deadline is not None and time.monotonic() >= deadline:
            return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
                args=commandline,
                returncode=pyfltr.command.process.TIMEOUT_RETURNCODE,
                stdout=proc.stdout + "\n実行時間の上限に達しました。lychee-timeoutを延長して再実行してください。\n",
                timeout_exceeded=True,
                retry_count=retry_count,
            )
    raise AssertionError("有限再試行は最終試行で結果を返す")
