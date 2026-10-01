"""lycheeの外部リンク一時障害の分類と有限再試行。"""

import json
import pathlib
import time
import typing
import urllib.parse

import pyfltr.command.process
import pyfltr.config.config


def is_transient_failure(entry: dict[str, typing.Any], *, timeout: bool = False) -> bool:
    """HTTP(S)の5xxまたは応答タイムアウトと確認できる場合に真を返す。"""
    url = entry.get("url")
    status = entry.get("status")
    if not isinstance(url, str) or not isinstance(status, dict):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if timeout:
        return status.get("text") == "Timeout"
    code = status.get("code")
    return isinstance(code, int) and not isinstance(code, bool) and 500 <= code < 600


def classify_failures(output: str) -> tuple[bool, bool] | None:
    """完全な失敗JSONから（一時障害のみか、5xxを含むか）を返す。

    非0終了の格下げに使うため、進捗や別のエラーが混入したJSONは救済しない。
    lycheeがstderrへ出力する通常のHint行だけを除き、残り全体をJSONとして読む。
    失敗件数とmapの全項目を比較し、欠落・分類不能を通常失敗として保持する。
    """
    try:
        data = json.loads("\n".join(line for line in output.splitlines() if not line.startswith("Hint: ")))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    counts = [data.get(key) for key in ("errors", "timeouts", "unknown", "unsupported")]
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in counts):
        return None
    if data["unknown"] or data["unsupported"]:
        return None
    transient: list[bool] = []
    has_server_error = False
    for map_name, count_name in (("error_map", "errors"), ("timeout_map", "timeouts")):
        entries_by_file = data.get(map_name)
        if not isinstance(entries_by_file, dict):
            return None
        count = 0
        for file_path, entries in entries_by_file.items():
            if not isinstance(file_path, str) or not file_path or not isinstance(entries, list):
                return None
            for entry in entries:
                if not isinstance(entry, dict):
                    return None
                count += 1
                is_timeout = map_name == "timeout_map"
                temporary = is_transient_failure(entry, timeout=is_timeout)
                transient.append(temporary)
                has_server_error |= temporary and not is_timeout
        if count != data[count_name]:
            return None
    if not transient:
        return None
    return all(transient), has_server_error


def run_lychee(
    commandline: list[str],
    config: pyfltr.config.config.Config,
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
    timeout = pyfltr.config.config.resolve_command_timeout(config.values, "lychee")
    deadline = time.monotonic() + timeout if timeout is not None and timeout > 0 else None
    retry_count = 0
    retry_options = pyfltr.config.config.resolve_retry_kwargs(config.values)
    oom_limit = max(0, retry_options["retry_max_attempts"]) if retry_options["retry_on_oom"] else 0
    for attempt in range(4):
        # OOMの再試行でも時間予算を更新し、各試行へ元の上限を与え直さない。
        oom_attempt = 0
        while True:
            remaining = max(0.001, deadline - time.monotonic()) if deadline is not None else timeout
            proc = pyfltr.command.process.run_traced_subprocess(
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
