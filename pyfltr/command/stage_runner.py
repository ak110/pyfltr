"""cli/pipeline.py / output/ui.py が共用するステージ実行ヘルパー。"""

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
import pyfltr.warnings_


def make_skipped_result(
    command: str,
    config: pyfltr.config.model.Config,
    *,
    reason: str | None = None,
) -> pyfltr.command.core_.CommandResult:
    """中断対象の skipped CommandResult を生成する。

    `reason`が指定された場合は`CommandResult.output`に反映する。省略時は既定の
    `--fail-fast`文言（従来互換）を使う。TUIのCtrl+C協調停止経路では固有の文言を
    渡して出力する。
    """
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
    """未開始ジョブをキャンセルし、中断対象コマンド名を`aborted_commands`に追加する。

    `done()`のfutureは対象外とする。`cancel()`がTrueを返した（キャンセル成功）
    ものだけを`aborted_commands`に登録する。
    """
    for future, command in future_to_command.items():
        if future.done():
            continue
        if future.cancel():
            aborted_commands.add(command)


@dataclasses.dataclass
class StageCallbacks:
    """表示と中断を実行制御へ渡すためのコールバック。"""

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


class SlotWaitReporter:
    """実行枠の待機区間の通知を受けて、待機の継続と再開を文面で表示する。

    コミットフックなどから起動した検証が実行枠を待つ間は出力が止まり、停止と区別できない。待機中であること、保持者、待機中の要求数、完了したツール数と、
    残り時間の見込みを示せない理由を一定間隔で表示する。
    見込みは、保持者と先に並んだ検証の残り処理時間をpyfltrが観測できないため算出しない。
    """

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
        # 省略時は生成時点のモジュール値を使う。結合試験が間隔を短縮して差し替えられるようにするため。
        self._delay = delay if delay is not None else _SLOT_PROGRESS_DELAY
        self._interval = interval if interval is not None else _SLOT_PROGRESS_INTERVAL
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._notified = False

    def __call__(self, wait: pyfltr.command.execution_slot.SlotWait) -> None:
        """待機区間の開始で継続の通知を始め、終了で止めて、通知済みなら再開を渡す。"""
        if wait.finished_at is None:
            self._start()
            return
        self._finish()
        if self._notified:
            self._notified = False
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
                self._notified = True
                self._notify(message)
                timeout = self._interval

        self._stop = stop
        self._thread = threading.Thread(target=loop, name="pyfltr-slot-wait", daemon=True)
        self._thread.start()

    def _finish(self) -> None:
        if self._stop is not None and self._thread is not None:
            self._stop.set()
            self._thread.join()
        self._stop = None
        self._thread = None


def _slot_waiting_message(elapsed: float, done: int, total: int) -> str:
    owner = pyfltr.command.execution_slot.read_owner()
    if owner is None:
        holder = "記録を読めません"
    else:
        holder = f"pid={owner.pid}、cwd={owner.cwd}" + (f"、{owner.label}" if owner.label else "")
        if not owner.is_alive():
            # 保持者が強制終了するとOSがロックを解放するが記録は残る。終了済みの記録を保持者と取り違えさせない。
            holder += "（記録上の保持者。このプロセスは終了しており、次の待機者へ順番が移る途中です）"
    waiters = pyfltr.command.execution_slot.waiter_count()
    return (
        "同じOSアカウントの別の検証が実行枠を保持しているため、実行枠の取得を待っています"
        f"（経過{elapsed:.0f}秒。保持者: {holder}。待機中の要求: 自分を含め{waiters}件。完了したツール: {done}/{total}件）。"
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
    """fix、直列formatter、LPT順の並列lint・testを共通の制御で実行する。

    fix段の結果は通常段の結果と同じツールの実行であり、成功時は保存だけを行い最終結果へ含めない。
    fix段が失敗した場合は、同じツールの通常段の結果（成功・スキップを含む）へ失敗と出力を統合した
    1件を最終結果とし、`collected`・`on_result`・`archive`へ同じ結果を渡す。
    fail-fastで停止する場合もfix段の失敗結果を記録し、同じツールのスキップ結果を重ねない。

    対象ファイルが存在し得ないツール（`pyfltr.command.executor.has_no_candidate_targets`）は実行枠の取得前に実行して
    結果を記録し、fix段からは除く。全ツールが該当する場合は実行枠を取得しない。
    残りのツールは検証の実行枠を取得してから実行し、全ツールを回収するまで枠を保持し続ける（途中では譲らない）。
    `callbacks.on_slot_progress`があれば、待機の継続と再開を`SlotWaitReporter`で逐次表示する。
    別の検証が枠を保持中なら警告を出力して待つ。TUIの中断（`is_interrupted`）で待機を止めた場合は全コマンドをスキップ扱いにする。
    枠の待機区間は`callbacks.on_slot_wait`へ通知する。
    実行した各ツールの結果には、`execute`の直前と直後の時刻を`started_at`・`finished_at`として設定する。
    """
    hooks = callbacks if callbacks is not None else StageCallbacks()
    collected = results if results is not None else []
    config = base_ctx.config
    execute = _with_body_interval(execute)
    fixers, formatters, parallel = pyfltr.command.executor.split_commands_for_execution(
        commands,
        config,
        base_ctx.all_files,
        include_fix_stage=include_fix_stage,
        subproject_configs=base_ctx.subproject_configs,
    )
    # 対象ファイルが存在し得ないツールは外部ツールを起動しないため、実行枠の取得前に実行して結果を確定する。
    # 文書だけの検証のように多くのツールが対象を持たない実行で、枠の取得・譲渡・取り直しの待ちを省くためである。
    # 実行枠の外で外部ツールを起動しないよう、判定は母集合とglobで0件が確定するものに限る
    # （`pyfltr.command.executor.has_no_candidate_targets`）。
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
        # 表示の停止を先に行い、保存の通知が例外を送出しても表示用のスレッドを残さない。
        if reporter is not None:
            reporter(wait)
        if hooks.on_slot_wait is not None:
            hooks.on_slot_wait(wait)

    def archive(result: pyfltr.command.core_.CommandResult) -> None:
        if hooks.archive is not None and not result.cached:
            hooks.archive(result)

    # fix段で失敗したツールの結果。通常段の結果を記録するときに統合する。
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
        # 通常段の対象に含まれないツールのfix段失敗も最終結果へ残す。
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
                # 例外（CLIのCtrl+Cなど）で抜ける場合も、起動中の子プロセスを止めてから実行枠を解放する。
                pyfltr.command.process.terminate_active_processes()
                raise
    except pyfltr.command.execution_slot.WaitAborted as error:
        # 待機の取消の理由を、未指定のfail-fastの案内で置き換えない。
        completed = {result.command for result in collected}
        skip([command for command in [*formatters, *parallel] if command not in completed], reason=str(error) or None)
        flush_fix_failures()
    return collected


def _with_body_interval(
    execute: collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult],
) -> collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult]:
    """`execute`の結果へツール本体区間の開始・終了時刻を設定する関数を返す。

    ツールのプロセスを起動しなかった結果（対象0件などのスキップで`returncode`が`None`の結果と、
    起動コマンドの解決に失敗した`resolution_failed`の結果）と、キャッシュから復元した結果は、本体を実行していないため設定しない。
    """

    def wrapped(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
        started_at = _now_iso()
        result = execute(command, fix_stage)
        if ran_tool(result):
            result.started_at = started_at
            result.finished_at = _now_iso()
        return result

    return wrapped


def ran_tool(result: pyfltr.command.core_.CommandResult) -> bool:
    """結果がツールのプロセスを起動して得たものかを返す。

    対象0件などのスキップ（`returncode`が`None`）、起動コマンドの解決に失敗した`resolution_failed`、
    キャッシュから復元した結果は、ツール本体を実行していない。
    本体区間の記録に使う。
    """
    return result.returncode is not None and not result.resolution_failed and not result.cached


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _notify_slot_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
    """実行枠の待機を始めたことを警告で通知する。"""
    holder = f"pid={owner.pid}、cwd={owner.cwd}" if owner is not None else "保持者の記録を読めません"
    pyfltr.warnings_.emit_warning(
        source="execution-slot",
        message=(
            f"同じOSアカウントで別の検証がツールを実行中のため（{holder}）、"
            "その終了を待ってからツールを実行します。待機時間はツールのtimeoutに含めません"
        ),
        hint=("対応不要です。待機をやめる場合は実行を中断してください（端末ではCtrl+C、MCPでは要求の取り消し）。"),
    )


def merge_fix_failure(
    fix_result: pyfltr.command.core_.CommandResult,
    result: pyfltr.command.core_.CommandResult,
) -> pyfltr.command.core_.CommandResult:
    """fix段の失敗を同じツールの通常段の結果へ統合する。

    状態と終了コードは`CommandResult.merge`の規則で集約し、fix段の失敗を保持する。
    再実行コマンドなど通常段で確定した値は通常段の結果から引き継ぐため、通常段を先頭にして集約する。
    出力は実行順にfix段、通常段の順で連結する。本体区間はfix段の開始から通常段の終了までとする（通常段がスキップならfix段の終了まで）。対象件数は同じ対象を2段で数えないよう通常段の値を使う。
    通常段が診断を出力した場合は通常段の診断だけを使う。fix段の診断は修正後の残存違反であり、通常段の診断と重複するためである。
    通常段が診断を出力しなかった場合（スキップや診断0件）はfix段の診断を残し、失敗を指すファイルと位置を保持する。
    """
    fix_part = dataclasses.replace(fix_result, errors=[]) if result.errors else fix_result
    merged = pyfltr.command.core_.CommandResult.merge([result, fix_part])
    return dataclasses.replace(
        merged,
        output="\n".join(output for output in (fix_result.output, result.output) if output),
        files=result.files or fix_result.files,
        started_at=fix_result.started_at,
        finished_at=result.finished_at or fix_result.finished_at,
    )
