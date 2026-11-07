"""cli/pipeline.py / output/ui.py が共用するステージ実行ヘルパー。"""

import collections.abc
import concurrent.futures
import contextlib
import dataclasses
import datetime as dt

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


_CHECKPOINT_INTERVAL = 0.5
"""参加券を渡したツールの実行中に、協調の譲渡点を呼ぶ間隔（秒）。ツールの停止通知から譲渡までの遅れの上限になる。"""


def run_stages(
    commands: list[str],
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    execute: collections.abc.Callable[[str, bool], pyfltr.command.core_.CommandResult],
    *,
    include_fix_stage: bool = False,
    fail_fast: bool = False,
    callbacks: StageCallbacks | None = None,
    results: list[pyfltr.command.core_.CommandResult] | None = None,
    participant_ticket: str | None = None,
) -> list[pyfltr.command.core_.CommandResult]:
    """fix、直列formatter、LPT順の並列lint・testを共通の制御で実行する。

    fix段の結果は通常段の結果と同じツールの実行であり、成功時は保存だけを行い最終結果へ含めない。
    fix段が失敗した場合は、同じツールの通常段の結果（成功・スキップを含む）へ失敗と出力を統合した
    1件を最終結果とし、`collected`・`on_result`・`archive`へ同じ結果を渡す。
    fail-fastで停止する場合もfix段の失敗結果を記録し、同じツールのスキップ結果を重ねない。

    検証の実行枠を保持し、待機者がいれば起動済みのツールと子プロセスの回収後に譲る。
    別の検証が枠を保持中なら警告を出力して待つ。TUIの中断（`is_interrupted`）で待機を止めた場合は全コマンドをスキップ扱いにする。
    枠の待機区間は`callbacks.on_slot_wait`へ通知する。
    外側が協調所有者、または参加券付きholdなら、同じ回収済みの区切りから協調譲渡を通知する。
    実行した各ツールの結果には、`execute`の直前と直後の時刻を`started_at`・`finished_at`として設定する。

    自ら枠を取得した場合と`participant_ticket`（上位から受け取った参加券）を渡した場合は協調所有者になり、
    `{command}-slot-ticket`を有効にしたツールの子プロセスへツール1件専用の参加券を環境変数で渡す。参加券を使ったツールが区切りで停止を通知すると、
    ツールの実行中も所有スレッドが定期的に譲渡点を呼んで譲る。参加しないツールが1件でも稼働していれば譲らない。
    同じスレッドの入れ子と祖先から継承した区間では参加券を渡さず、従来どおりツールを回収した区切りでだけ譲る。
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

    def checkpoint() -> None:
        pyfltr.command.execution_slot.yield_to_waiters()

    # 協調所有者のときだけ値を持つ。ツールの実行中の譲渡点と参加券の発行に使う。
    coordinator: pyfltr.command.execution_slot.SlotCoordinator | None = None

    def run_with_ticket(command: str, fix_stage: bool, ticket: str | None) -> pyfltr.command.core_.CommandResult:
        with pyfltr.command.execution_slot.bind_tool_ticket(ticket):
            return execute(command, fix_stage)

    def submit(
        executor: concurrent.futures.ThreadPoolExecutor, command: str, fix_stage: bool
    ) -> tuple[concurrent.futures.Future, contextlib.ExitStack]:
        # 参加券の登録はツールの起動前に所有スレッドで行い、ツールの回収後に閉じる。
        # 登録中の参加者は停止を通知するまで稼働中とみなされ、参加しないツールの実行中に譲らないためである。
        registration = contextlib.ExitStack()
        ticket = registration.enter_context(coordinator.participant()) if coordinator is not None else None
        # 参加券は受け取ると宣言したツールにだけ渡す。他のツールは登録のまま停止を通知しないため、稼働中は譲らない。
        passed = ticket if pyfltr.config.model.resolve_slot_ticket(config.values, command) else None
        try:
            future = executor.submit(run_with_ticket, command, fix_stage, passed)
        except BaseException:
            registration.close()
            raise
        return future, registration

    def run_serial(
        executor: concurrent.futures.ThreadPoolExecutor, command: str, fix_stage: bool
    ) -> pyfltr.command.core_.CommandResult:
        if coordinator is None:
            return execute(command, fix_stage)
        future, registration = submit(executor, command, fix_stage)
        with registration:
            while _next_completed([future], _CHECKPOINT_INTERVAL) is None:
                checkpoint()
            return future.result()

    def execute_stages(serial_executor: concurrent.futures.ThreadPoolExecutor) -> None:
        for command in fixers:
            result = run_serial(serial_executor, command, True)
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

            checkpoint()

        for index, command in enumerate(formatters):
            result = run_serial(serial_executor, command, False)
            record(result, "formatter")
            if hooks.is_interrupted() or (fail_fast and result.failed):
                skip([*formatters[index + 1 :], *parallel])
                return
            if index + 1 < len(formatters) or parallel:
                checkpoint()

        if parallel:
            aborted = False
            aborted_commands: set[str] = set()
            pending = list(parallel)
            with (
                concurrent.futures.ThreadPoolExecutor(max_workers=config["jobs"]) as executor,
                contextlib.ExitStack() as registrations,
            ):
                future_to_command: dict[concurrent.futures.Future, str] = {}
                future_to_registration: dict[concurrent.futures.Future, contextlib.ExitStack] = {}
                while pending or future_to_command:
                    while (
                        pending
                        and len(future_to_command) < config["jobs"]
                        and not aborted
                        and not hooks.is_interrupted()
                        and (not future_to_command or not pyfltr.command.execution_slot.yield_requested())
                    ):
                        command = pending.pop(0)
                        future, registration = submit(executor, command, False)
                        future_to_command[future] = command
                        future_to_registration[future] = registrations.enter_context(registration)
                    if not future_to_command:
                        if aborted or hooks.is_interrupted():
                            aborted_commands.update(pending)
                            break
                        continue
                    future = _next_completed(future_to_command, _CHECKPOINT_INTERVAL if coordinator is not None else None)
                    if future is None:
                        # 参加券を渡したツールが区切りで停止していれば、ツールの実行中でも譲る
                        checkpoint()
                        continue
                    command = future_to_command.pop(future)
                    future_to_registration.pop(future).close()
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
                    if pending and not future_to_command and not aborted and not hooks.is_interrupted():
                        # 再取得後は最低1件を開始し、互いに実行前の譲渡だけを繰り返す状態を防ぐ。
                        checkpoint()
            if aborted_commands:
                skip(list(aborted_commands))
        flush_fix_failures()

    try:
        with (
            pyfltr.command.execution_slot.coordinated_hold(
                should_abort=hooks.is_interrupted,
                on_wait=_notify_slot_wait,
                on_wait_interval=hooks.on_slot_wait,
                participant=participant_ticket,
            ) as coordinator,
            concurrent.futures.ThreadPoolExecutor(max_workers=1) as serial_executor,
        ):
            try:
                execute_stages(serial_executor)
            except BaseException:
                # 例外（CLIのCtrl+Cなど）で抜ける場合も、起動中の子プロセスを止めてから実行枠を解放する。
                pyfltr.command.process.terminate_active_processes()
                raise
    except pyfltr.command.execution_slot.WaitAborted as error:
        # 外側の協調区間から伝わる中断もあり、未指定のfail-fastの案内で原因を置き換えない。
        completed = {result.command for result in collected}
        skip([command for command in [*formatters, *parallel] if command not in completed], reason=str(error) or None)
        flush_fix_failures()
    return collected


def _next_completed(
    futures: collections.abc.Iterable[concurrent.futures.Future], timeout: float | None
) -> concurrent.futures.Future | None:
    """最初に完了したFutureを返す。`timeout`秒以内に完了しなければ`None`を返す。"""
    try:
        return next(concurrent.futures.as_completed(futures, timeout=timeout))
    except TimeoutError:
        return None


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
        if result.returncode is not None and not result.resolution_failed and not result.cached:
            result.started_at = started_at
            result.finished_at = _now_iso()
        return result

    return wrapped


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
