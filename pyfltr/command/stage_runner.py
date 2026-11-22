import collections.abc
import concurrent.futures
import dataclasses
import datetime as dt
import threading
import time

import pyfltr.command.core_
import pyfltr.command.execution_slot
import pyfltr.command.executor
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.timestamps
import pyfltr.warnings_


def make_skipped_result(
    command: str,
    config: pyfltr.config.model.Config,
    *,
    reason: str | None = None,
) -> pyfltr.command.core_.CommandResult:
    command_info = config.commands[command]
    output = (
        reason
        if reason is not None
        else "--fail-fast により実行をスキップしました。先に失敗したコマンドの指摘を修正して再実行してください。"
    )
    return pyfltr.command.core_.CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=[],
        returncode=None,
        files=0,
        output=output,
        elapsed=0.0,
    )


def cancel_pending_futures(
    future_to_command: dict[concurrent.futures.Future, str],
    aborted_commands: set[str],
) -> None:
    for future, command in future_to_command.items():
        if future.done():
            continue
        if future.cancel():
            aborted_commands.add(command)


@dataclasses.dataclass
class StageCallbacks:
    archive: collections.abc.Callable[[pyfltr.command.core_.CommandResult], None] | None = None
    on_result: collections.abc.Callable[[pyfltr.command.core_.CommandResult], None] | None = None
    is_interrupted: collections.abc.Callable[[], bool] = lambda: False
    on_interrupted: collections.abc.Callable[[str], None] | None = None
    replace_result: (
        collections.abc.Callable[[pyfltr.command.core_.CommandResult, str], pyfltr.command.core_.CommandResult] | None
    ) = None
    on_skipped: collections.abc.Callable[[str], None] | None = None
    on_slot_wait: pyfltr.command.execution_slot.WaitIntervalCallback | None = None
    """実行枠の待機区間（`SlotWait`）の開始と終了を受け取る通知。実行アーカイブへの保存に使う。"""
    on_slot_progress: collections.abc.Callable[[str], None] | None = None
    """実行枠の待機の継続と再開を利用者へ逐次表示する1行の文面を受け取る通知。

    待機が`_SLOT_PROGRESS_DELAY`秒を超えた時点と、その後`_SLOT_PROGRESS_INTERVAL`秒ごとに継続を、
    通知を出した待機が終わった時点で再開を渡す。
    `pyfltr.warnings_.emit_warning`は要約へ蓄積されるため、待機中の逐次表示には使わない。
    """


_SLOT_PROGRESS_DELAY = 10.0
"""待機の継続を最初に表示するまでの秒数。短い待機のたびに出力が増えないよう、この時間を超えた待機だけを表示する。"""

_SLOT_PROGRESS_INTERVAL = 30.0
"""待機の継続を繰り返し表示する間隔（秒）。パイプラインのheartbeatのしきい値に揃える。"""


@dataclasses.dataclass
class _Waiting:
    stop: threading.Event
    thread: threading.Thread
    notified: bool = False


class SlotWaitReporter:
    def __init__(
        self,
        notify: collections.abc.Callable[[str], None],
        progress: collections.abc.Callable[[], tuple[int, int]],
        *,
        delay: float | None = None,
        interval: float | None = None,
    ) -> None:
        self._notify = notify
        self._progress = progress
        self._delay = delay if delay is not None else _SLOT_PROGRESS_DELAY
        self._interval = interval if interval is not None else _SLOT_PROGRESS_INTERVAL
        self._wait: _Waiting | None = None

    def __call__(self, wait: pyfltr.command.execution_slot.SlotWait) -> None:
        if wait.finished_at is None:
            self._start()
            return
        if self._finish():
            self._notify(_slot_resume_message(wait))

    def _start(self) -> None:
        self._finish()
        stop = threading.Event()
        started = time.monotonic()

        def loop() -> None:
            timeout = self._delay
            while not stop.wait(timeout):
                done, total = self._progress()
                message = _slot_waiting_message(time.monotonic() - started, done, total)
                if stop.is_set():
                    return
                self._notify(message)
                waiting.notified = True
                timeout = self._interval

        waiting = _Waiting(stop, threading.Thread(target=loop, name="pyfltr-slot-wait", daemon=True))
        self._wait = waiting
        waiting.thread.start()

    def _finish(self) -> bool:
        waiting = self._wait
        if waiting is None:
            return False
        waiting.stop.set()
        waiting.thread.join()
        self._wait = None
        return waiting.notified


def _slot_waiting_message(elapsed: float, done: int, total: int) -> str:
    owners = pyfltr.command.execution_slot.read_owners()
    holder = describe_slot_owners(owners)
    waiters = pyfltr.command.execution_slot.waiter_count()
    return (
        "同じOSアカウントの別の検証が実行枠を保持しているため、実行枠の取得を待っています"
        f"（経過{elapsed:.0f}秒。保持者: {holder}。待機中の要求: 自分を含め{waiters}件。完了したツール: {done}/{total}件）。"
        f"{describe_slot_owner_records(owners)}"
        "残り時間の見込みは、保持者と先に並んだ検証の残り処理時間を観測できないため示せません。"
        "停止ではないため待てば続行します。待機をやめる場合は実行を中断してください（端末ではCtrl+C、MCPでは要求の取り消し）。"
    )


def _slot_resume_message(wait: pyfltr.command.execution_slot.SlotWait) -> str:
    elapsed = f"待機{wait.elapsed:.0f}秒" if wait.elapsed is not None else "待機時間は不明"
    if wait.outcome in ("acquired", "inherited"):
        return f"実行枠を取得しました（{elapsed}）。ツールの実行を始めます。"
    return f"実行枠の待機を終えました（{elapsed}、理由: {wait.outcome}）。残りのツールはスキップされます。"


def run_stages(
    commands: list[str],
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    execute: collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult],
    *,
    include_fix_stage: bool = False,
    fail_fast: bool = False,
    callbacks: StageCallbacks | None = None,
    results: list[pyfltr.command.core_.CommandResult] | None = None,
) -> list[pyfltr.command.core_.CommandResult]:
    hooks = callbacks if callbacks is not None else StageCallbacks()
    collected = results if results is not None else []
    config = base_ctx.config
    execute = _with_tester_launch_boundary(_with_body_interval(execute), config)
    fixers, formatters, parallel = pyfltr.command.executor.split_commands_for_execution(
        commands,
        config,
        base_ctx.all_files,
        include_fix_stage=include_fix_stage,
        subproject_configs=base_ctx.subproject_configs,
    )
    idle = {
        command for command in [*formatters, *parallel] if pyfltr.command.executor.has_no_candidate_targets(command, base_ctx)
    }
    fixers = [command for command in fixers if command not in idle]
    idle_formatters = [command for command in formatters if command in idle]
    idle_parallel = [command for command in parallel if command in idle]
    formatters = [command for command in formatters if command not in idle]
    parallel = [command for command in parallel if command not in idle]
    total = len(idle_formatters) + len(idle_parallel) + len(formatters) + len(parallel)
    reporter = (
        SlotWaitReporter(hooks.on_slot_progress, lambda: (len(collected), total))
        if hooks.on_slot_progress is not None
        else None
    )

    def on_wait_interval(wait: pyfltr.command.execution_slot.SlotWait) -> None:
        if reporter is not None:
            reporter(wait)
        if hooks.on_slot_wait is not None:
            hooks.on_slot_wait(wait)

    def archive(result: pyfltr.command.core_.CommandResult) -> None:
        if hooks.archive is not None and not result.cached:
            hooks.archive(result)

    fix_failures: dict[str, pyfltr.command.core_.CommandResult] = {}

    def record(result: pyfltr.command.core_.CommandResult, stage: str) -> None:
        if hooks.replace_result is not None:
            result = hooks.replace_result(result, stage)
        fix_failure = fix_failures.pop(result.command, None)
        if fix_failure is not None:
            result = merge_fix_failure(fix_failure, result)
        collected.append(result)
        archive(result)
        if hooks.on_result is not None:
            hooks.on_result(result)

    def skip(remaining: list[str], *, reason: str | None = None) -> None:
        pyfltr.command.process.terminate_active_processes()
        reason = "Ctrl+C により中断しました。" if hooks.is_interrupted() else reason
        for command in remaining:
            record(make_skipped_result(command, config, reason=reason), "skipped")
            if hooks.on_skipped is not None:
                hooks.on_skipped(command)

    def flush_fix_failures() -> None:
        for command in list(fix_failures):
            record(make_skipped_result(command, config, reason=""), "skipped")

    def execute_stages() -> None:
        for command in fixers:
            result = execute(command, True)
            if result.failed:
                fix_failures[command] = result
            else:
                archive(result)
            if hooks.is_interrupted():
                if hooks.on_interrupted is not None:
                    hooks.on_interrupted(command)
                skip([*formatters, *parallel])
                flush_fix_failures()
                return
            if fail_fast and result.failed:
                skip([*formatters, *parallel])
                flush_fix_failures()
                return

        for index, command in enumerate(formatters):
            result = execute(command, False)
            record(result, "formatter")
            if hooks.is_interrupted() or (fail_fast and result.failed):
                skip([*formatters[index + 1 :], *parallel])
                return

        if parallel:
            aborted = False
            aborted_commands: set[str] = set()
            with concurrent.futures.ThreadPoolExecutor(max_workers=config["jobs"]) as executor:
                future_to_command = {executor.submit(execute, command, False): command for command in parallel}
                for future in concurrent.futures.as_completed(future_to_command):
                    command = future_to_command[future]
                    try:
                        result = future.result()
                    except concurrent.futures.CancelledError:
                        aborted_commands.add(command)
                        continue
                    record(result, "parallel")
                    if fail_fast and not aborted and result.failed:
                        aborted = True
                        cancel_pending_futures(future_to_command, aborted_commands)
                        pyfltr.command.process.terminate_active_processes()
                    if hooks.is_interrupted():
                        cancel_pending_futures(future_to_command, aborted_commands)
            if aborted_commands:
                skip(list(aborted_commands))
        flush_fix_failures()

    for command in idle_formatters:
        record(execute(command, False), "formatter")
    for command in idle_parallel:
        record(execute(command, False), "parallel")
    if not fixers and not formatters and not parallel:
        flush_fix_failures()
        return collected

    try:
        with pyfltr.command.execution_slot.hold(
            should_abort=hooks.is_interrupted,
            on_wait=_notify_slot_wait,
            on_wait_interval=on_wait_interval if reporter is not None or hooks.on_slot_wait is not None else None,
        ):
            try:
                execute_stages()
            except BaseException:
                pyfltr.command.process.terminate_active_processes()
                raise
    except pyfltr.command.execution_slot.WaitAborted as error:
        completed = {result.command for result in collected}
        skip([command for command in [*formatters, *parallel] if command not in completed], reason=str(error) or None)
        flush_fix_failures()
    return collected


def _with_body_interval(
    execute: collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult],
) -> collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult]:

    def wrapped(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        started_at = pyfltr.timestamps.now_iso()
        result = execute(command, fix_stage)
        if ran_tool(result):
            result.started_at = started_at
            result.finished_at = pyfltr.timestamps.now_iso()
        return result

    return wrapped


_MTIME_RESOLUTION = dt.timedelta(seconds=1)
"""テスターの開始境界で経過を待つ、ファイル更新時刻の粗い区間の幅。

秒単位で更新時刻を保持するファイルシステムを前提とする。
"""


def _with_tester_launch_boundary(
    execute: collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult],
    config: pyfltr.config.model.Config,
) -> collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult]:
    last_write_end: list[dt.datetime] = []

    def wrapped(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        command_type = config.commands[command].type
        if command_type != "tester" or fix_stage:
            result = execute(command, fix_stage)
            if (fix_stage or command_type == "formatter") and ran_tool(result) and result.finished_at is not None:
                last_write_end.append(dt.datetime.fromisoformat(result.finished_at))
            return result
        if last_write_end:
            latest = max(last_write_end)
            boundary = latest.replace(microsecond=0) + _MTIME_RESOLUTION
            while (remaining := (boundary - dt.datetime.now(dt.UTC)).total_seconds()) > 0:
                time.sleep(remaining)
        launch_boundary_at = pyfltr.timestamps.now_iso()
        result = execute(command, fix_stage)
        if ran_tool(result):
            result.launch_boundary_at = launch_boundary_at
        return result

    return wrapped


def ran_tool(result: pyfltr.command.core_.CommandResult) -> bool:
    return result.returncode is not None and not result.resolution_failed and not result.cached


def describe_slot_owner(owner: pyfltr.command.execution_slot.SlotOwner | None) -> str:
    if owner is None:
        return "保持者の記録を読めません"
    holder = f"pid={owner.pid}、cwd={owner.cwd}、取得={owner.acquired_at}"
    if not owner.is_alive():
        holder += "。記録上の保持者で、このプロセスは終了しており、次の待機者へ順番が移る途中です"
    return holder


def describe_slot_owners(owners: list[pyfltr.command.execution_slot.SlotOwner]) -> str:
    if not owners:
        return describe_slot_owner(None)
    return " ／ ".join(describe_slot_owner(owner) for owner in owners)


def describe_slot_owner_records(owners: list[pyfltr.command.execution_slot.SlotOwner]) -> str:
    if not owners:
        return ""
    lock_dir = pyfltr.command.execution_slot.default_lock_dir()
    return (
        f"保持者のlabel全文は{lock_dir}内のexecution.owner.json・execution-<番号>.owner.jsonで確認できます"
        "（保持者が枠を解放すると記録は除去されます）。"
    )


def _notify_slot_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
    owners = pyfltr.command.execution_slot.read_owners() or ([owner] if owner is not None else [])
    pyfltr.warnings_.emit_warning(
        source="execution-slot",
        message=(
            f"同じOSアカウントの別の検証が実行枠を保持しているため（{describe_slot_owners(owners)}）、"
            "その解放を待ってからツールを実行します。待機時間はツールのtimeoutに含めません。"
            f"{describe_slot_owner_records(owners)}"
        ),
        hint=("対応不要です。待機をやめる場合は実行を中断してください（端末ではCtrl+C、MCPでは要求の取り消し）。"),
    )


def merge_fix_failure(
    fix_result: pyfltr.command.core_.CommandResult,
    result: pyfltr.command.core_.CommandResult,
) -> pyfltr.command.core_.CommandResult:
    fix_part = dataclasses.replace(fix_result, errors=[]) if result.errors else fix_result
    merged = pyfltr.command.core_.CommandResult.merge([result, fix_part])
    return dataclasses.replace(
        merged,
        output="\n".join(output for output in (fix_result.output, result.output) if output),
        files=result.files or fix_result.files,
        started_at=fix_result.started_at,
        finished_at=result.finished_at or fix_result.finished_at,
    )
