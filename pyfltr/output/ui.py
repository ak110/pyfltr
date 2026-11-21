"""Textual UI関連の処理。"""

import contextlib
import dataclasses
import logging
import sys
import threading
import time
import typing

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Log, TabbedContent, TabPane

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.execution_slot
import pyfltr.command.executor
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.slow_tests
import pyfltr.command.stage_runner
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.selection
import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.run_options
import pyfltr.tools
import pyfltr.warnings_


def _format_errors_tab_label(error_count: int, warning_count: int) -> str:
    """Errors タブのラベル文字列を組み立てる。"""
    if warning_count:
        return f"Errors ({error_count}/{warning_count}w)"
    return f"Errors ({error_count})"


def run_commands_with_ui(
    commands: list[str],
    args: pyfltr.run_options.RunOptions,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    slot_wait_hook: pyfltr.command.execution_slot.WaitIntervalCallback | None = None,
    on_result: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    fail_fast: bool = False,
    only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None = None,
) -> tuple[list[pyfltr.command.core_.CommandResult], int]:
    """UI付きでコマンドを実行。

    `base_ctx`はパイプライン全体で不変のコンテキスト（config・all_files・cache_store・
    cache_run_idを含む）。各コマンド実行前に`ExecutionContext`を組み立てて渡す。

    `archive_hook`が指定されている場合、各コマンド完了時に実行アーカイブへ書き込む
    （fixステージも含めて全実行を保存する）。キャッシュヒット時の結果はアーカイブには
    書き込まない（`cached_from`でソースrunを参照させる前提）。
    `slot_wait_hook`は実行枠の待機区間の開始と終了を受け取る。

    `on_result`が指定されている場合、通常ステージ（formatter / linter / tester）の
    各コマンド完了時にarchive_hookの後に呼ぶ。fixステージでは呼ばない。

    `fail_fast=True`のとき、いずれかのツールが`status`として`failed`または`resolution_failed`を返した時点で
    未実行ジョブを`future.cancel()`で打ち切り、起動済みサブプロセスに
    `terminate()`を送る。

    `only_failed_targets`が指定された場合、ツール別の`ToolTargets`を
    `execute_command`へ渡す（`--only-failed`経路）。実対象の決め方は
    `ToolTargets.resolve_files()`に従う。
    """
    app = UIApp(
        commands,
        args,
        base_ctx,
        archive_hook=archive_hook,
        slot_wait_hook=slot_wait_hook,
        on_result=on_result,
        fail_fast=fail_fast,
        only_failed_targets=only_failed_targets,
    )
    try:
        return_code = app.run()
        if return_code is None:
            return_code = 0
        else:
            assert isinstance(return_code, int)

        return app.results, return_code
    except Exception as exc:
        # Textualアプリケーション自体の例外処理。
        # 詳細traceback全文の画面表示は避け、利用者には1行サマリのみを示す。
        # 詳細はlogger経由（exc_info=True）でログハンドラー側に記録させる。
        logging.error("UI アプリケーションの実行に失敗しました: %s", exc, exc_info=True)
        print(
            f"エラー: UI アプリケーションの実行に失敗しました: {exc}。`--no-ui` を指定するとUIを使わずに実行できます",
            file=sys.stderr,
        )
        sys.exit(1)


# ステータス表示用の定義
_STATUS_DISPLAY: dict[str, str] = {
    "waiting": "○ waiting",
    "running": "● running",
    "succeeded": "✓ done",
    "failed": "⚠ failed",
    "resolution_failed": "⚠ resolution failed",
    "formatted": "△ formatted",
    "skipped": "- skipped",
}


@dataclasses.dataclass
class _Running:
    active: set[str] = dataclasses.field(default_factory=set)
    last_press: float | None = None


@dataclasses.dataclass(frozen=True)
class _InterruptResults:
    snapshot: frozenset[str]
    commands: dict[str, None] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class _Stopping:
    results: _InterruptResults
    last_press: float | None = None


@dataclasses.dataclass(frozen=True)
class _Exited:
    results: _InterruptResults


class UIApp(App):
    """Textualアプリケーション。"""

    CSS = """
    #summary-table {
        height: 1fr;
    }
    Log.output {
        scrollbar-gutter: stable;
    }
    """

    def __init__(
        self,
        commands: list[str],
        args: pyfltr.run_options.RunOptions,
        base_ctx: pyfltr.command.core_.ExecutionBaseContext,
        *,
        archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
        slot_wait_hook: pyfltr.command.execution_slot.WaitIntervalCallback | None = None,
        on_result: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
        fail_fast: bool = False,
        only_failed_targets: dict[str, pyfltr.command.only_failed.ToolTargets] | None = None,
    ) -> None:
        super().__init__()
        self.commands = commands
        self.args = args
        self._base_ctx = base_ctx
        # base_ctxの頻出フィールドをショートカットとして保持する
        self.config = base_ctx.config
        self._all_files = base_ctx.all_files
        self._archive_hook = archive_hook
        self._slot_wait_hook = slot_wait_hook
        self._on_result = on_result
        self._fail_fast = fail_fast
        self._only_failed_targets = only_failed_targets
        self.results: list[pyfltr.command.core_.CommandResult] = []
        self.lock = threading.Lock()
        self.ctrl_c_timeout: float = 1.0
        self._start_times: dict[str, float] = {}
        self._phase: _Running | _Stopping | _Exited = _Running()
        # エラー蓄積用（Errorsタブの即時更新に使用）
        self._all_errors: list[pyfltr.diagnostics.ErrorLocation] = []
        self._errors_tab_exists = False

    def _enabled_commands(self) -> list[str]:
        """実行対象として有効なコマンド一覧を返す。

        モノレポでは起点と各サブプロジェクトの和集合で判定する（タブ生成とサマリー行追加で共用）。
        """
        return [
            cmd
            for cmd in self.commands
            if pyfltr.config.selection.is_command_enabled_anywhere(cmd, self.config, self._base_ctx.subproject_configs)
        ]

    def compose(self) -> ComposeResult:
        """UIを構成。"""
        with TabbedContent(initial="summary"):
            with TabPane("Summary", id="summary"):
                yield DataTable(id="summary-table")

            # 有効なコマンドのみタブを作成
            # (Errorsタブはエラー発生時にsummaryの直後に動的追加)
            for command in self._enabled_commands():
                with TabPane(command, id=f"tab-{command}"):
                    yield Log(id=f"output-{command}", classes="output")

    def on_ready(self) -> None:
        """ready時の処理。"""
        # Summaryテーブルの初期化
        table = self.query_one("#summary-table", DataTable)
        table.add_column("Command", key="command", width=20)
        table.add_column("Status", key="status", width=16)
        table.add_column("Errors", key="errors", width=8)
        table.add_column("Time", key="time", width=10)

        for command in self._enabled_commands():
            table.add_row(
                command,
                _STATUS_DISPLAY["waiting"],
                "-",
                "-",
                key=command,
            )

        # 経過時間の定期更新
        self.set_interval(1.0, self._update_elapsed_times)

        # コマンド実行をバックグラウンドで開始
        self.set_timer(0.1, self._run_commands)

    def _press_ctrl_c(self, now: float) -> typing.Literal["prompt", "stop", "exit"] | None:
        with self.lock:
            phase = self._phase
            if isinstance(phase, _Exited):
                return None
            if phase.last_press is None or now - phase.last_press > self.ctrl_c_timeout:
                phase.last_press = now
                return "prompt"
            if isinstance(phase, _Running):
                self._phase = _Stopping(_InterruptResults(frozenset(phase.active)))
                return "stop"
            self._phase = _Exited(phase.results)
            return "exit"

    def _finish_interrupt(self) -> bool:
        with self.lock:
            if isinstance(self._phase, _Stopping):
                self._phase = _Exited(self._phase.results)
                return True
            return False

    def _track_subprocess(self, command: str, *, started: bool) -> None:
        # UI反映中は実行中とみなさず、協調中断に入った後はスナップショットを変えない。
        with self.lock:
            phase = self._phase
            if isinstance(phase, _Running):
                if started:
                    phase.active.add(command)
                else:
                    phase.active.discard(command)

    def _is_interrupted(self) -> bool:
        return not isinstance(self._phase, _Running)

    def on_key(self, event) -> None:
        """時間窓内の2回押しで協調中断し、その後の別の2回押しで強制終了する。"""
        if event.key != "ctrl+c":
            return
        action = self._press_ctrl_c(time.monotonic())
        if action == "prompt":
            self.notify("終了するには 1 秒以内にもう一度 Ctrl+C を押してください。")
        elif action == "stop":
            pyfltr.command.process.terminate_active_processes()
            self.notify("中断処理中です。完了済みツールの結果をまとめています...")
        elif action == "exit":
            pyfltr.command.process.terminate_active_processes()
            self.exit(return_code=130)

    def _safe_call_from_thread(self, callback, *args, **kwargs) -> None:
        """イベントループ喪失時に抑止する`call_from_thread`ラッパー。

        協調停止ではUIイベントループが閉じた後もBGスレッドが短時間実行し続ける設計だが、
        Textualはイベントループ喪失時に例外を送出する。BGスレッドはここで結果を
        `self.results`に蓄積する責務があるため、UI反映側の失敗でBGスレッドを
        停止させないようにする。

        終了要求済みのときはUI反映を完全に短絡する。閉じつつあるイベントループへ
        `call_from_thread`すると`run_coroutine_threadsafe(...).result()`で長引き、
        workerスレッドが`ThreadPoolExecutor`の`shutdown(wait=True)`にたどり着けなくなる
        病理を避けるため。
        """
        if isinstance(self._phase, _Exited):
            return
        try:
            self.call_from_thread(callback, *args, **kwargs)
        except Exception:
            logger = logging.getLogger(__name__)
            logger.debug("call_from_thread 失敗（イベントループ喪失の可能性）", exc_info=True)

    def _update_elapsed_times(self) -> None:
        """running中のコマンドの経過時間を更新。"""
        table = self.query_one("#summary-table", DataTable)
        for command, start_time in self._start_times.items():
            elapsed = time.perf_counter() - start_time
            table.update_cell(command, "time", f"{elapsed:.1f}s…")

    def _update_summary(self, command: str, status: str, error_count: int | None = None, elapsed: float | None = None) -> None:
        """Summaryテーブルの行を更新。"""
        table = self.query_one("#summary-table", DataTable)
        table.update_cell(command, "status", _STATUS_DISPLAY.get(status, status))
        if error_count is not None:
            table.update_cell(command, "errors", str(error_count))
        if elapsed is not None:
            table.update_cell(command, "time", f"{elapsed:.1f}s")

    def _run_commands(self) -> None:
        """バックグラウンドでコマンドを実行。"""
        threading.Thread(target=self._run_in_background, daemon=True).start()

    def _run_in_background(self):
        """バックグラウンド処理。"""
        try:

            def execute(command: str, fix_stage: bool) -> pyfltr.command.core_.CommandResult:
                return self._execute_command(command, fix_stage=fix_stage)

            pyfltr.command.stage_runner.run_stages(
                self.commands,
                self._base_ctx,
                execute,
                include_fix_stage=self.args.include_fix_stage,
                fail_fast=self._fail_fast,
                callbacks=pyfltr.command.stage_runner.StageCallbacks(
                    archive=self._archive_hook,
                    on_slot_wait=self._slot_wait_hook,
                    on_result=self._on_result,
                    is_interrupted=self._is_interrupted,
                    on_interrupted=self._record_interrupted,
                    replace_result=self._replace_interrupted_result,
                    on_skipped=self._show_skipped,
                ),
                results=self.results,
            )

            # 中断時はwarnings欄に中断通知を1行出力する。
            with self.lock:
                phase = self._phase
                interrupted_ordered = list(phase.results.commands) if isinstance(phase, _Stopping) else []
            if isinstance(phase, _Exited):
                return
            if isinstance(phase, _Stopping):
                # config.command_names順に並べ替えて一意化（UI定義順）。
                interrupted_sorted = sorted(
                    interrupted_ordered,
                    key=lambda command: pyfltr.tools.command_index(self.config.command_names, command),
                )
                if interrupted_sorted:
                    names = ", ".join(interrupted_sorted)
                    pyfltr.warnings_.emit_warning(
                        source="pyfltr",
                        message=f"Ctrl+C により中断しました。中断されたツール: {names}",
                        hint="中断要求を処理したため対応不要です。検査を完了する場合は再実行してください。",
                    )
                else:
                    pyfltr.warnings_.emit_warning(
                        source="pyfltr",
                        message="Ctrl+C により中断しました。",
                        hint="中断要求を処理したため対応不要です。検査を完了する場合は再実行してください。",
                    )
                if self._finish_interrupt():
                    try:
                        self.call_from_thread(self.exit, return_code=130)
                    except Exception:
                        logging.getLogger(__name__).debug(
                            "call_from_thread(self.exit) 失敗（イベントループ喪失の可能性）",
                            exc_info=True,
                        )
                return

            # 自動終了判定
            overall_status = pyfltr.command.core_.overall_status(self.results)

            # FORMATTED/SUCCESSの場合は自動終了（--keep-ui時は終了しない）
            if overall_status != "FAILED" and not self.args.keep_ui:
                self._safe_call_from_thread(self.exit)

        except Exception as exc:
            # Textualエラー時の処理。詳細はlogger（exc_info=True）に記録し、画面側は1行サマリのみ。
            error_msg = f"UI 処理中に致命的エラーが発生しました: {exc}。`--no-ui` を指定するとUIを使わずに実行できます"
            logging.error("UI 処理中に致命的エラーが発生しました: %s", exc, exc_info=True)
            # call_from_thread自体が失敗した場合は既にlogging済みのため追加処理不要。
            with contextlib.suppress(Exception):
                self.call_from_thread(self._handle_fatal_error, error_msg)

    def _execute_command(self, command: str, *, fix_stage: bool = False) -> pyfltr.command.core_.CommandResult:
        """出力をキャプチャしながらコマンド実行。"""
        # 中断済みならsubprocessを一切起動せずskippedで返す（早期離脱）。
        if self._is_interrupted():
            self._record_interrupted(command)
            return pyfltr.command.stage_runner.make_skipped_result(command, self.config, reason="Ctrl+C により中断しました。")

        # serial_groupを持つコマンドは同一グループ内で排他実行される（cargo / dotnet等）。
        # ロック取得前は「待機中」の表示に留め、running表示はロック取得後に切り替える。
        with pyfltr.command.executor.serial_group_lock(self.config.commands[command].serial_group):
            # ロック取得後の再チェック。serial_group待機中にCtrl+Cを受け取った場合、
            # ロック取得後にsubprocessを起動せずskippedで返すことで協調停止前提を保つ。
            if self._is_interrupted():
                self._record_interrupted(command)
                return pyfltr.command.stage_runner.make_skipped_result(
                    command, self.config, reason="Ctrl+C により中断しました。"
                )

            # Summaryを「running」に更新
            self._start_times[command] = time.perf_counter()
            self._safe_call_from_thread(self._update_summary, command, "running")

            # コマンドタブに開始メッセージを出力
            self._safe_call_from_thread(
                self._write_log,
                f"#output-{command}",
                f"{command} を実行中です...\n",
            )

            # JSONパーサー対応ツールではストリーミング出力を抑制し、
            # 完了後にErrorLocationベースの表示に切り替える。
            has_custom_parser = command in pyfltr.parsing.entry.get_custom_parser_commands()
            callback: typing.Callable[[str], None] | None = None
            if not has_custom_parser:

                def _on_output(line: str) -> None:
                    """出力行をリアルタイムでUIに反映。"""
                    self._safe_call_from_thread(self._write_log, f"#output-{command}", line.removesuffix("\n"))

                callback = _on_output

            def _on_subprocess_start() -> None:
                self._track_subprocess(command, started=True)

            def _on_subprocess_end() -> None:
                self._track_subprocess(command, started=False)

            try:
                ctx = pyfltr.command.core_.ExecutionContext(
                    base=self._base_ctx,
                    fix_stage=fix_stage,
                    only_failed_targets=pyfltr.command.targets.pick_targets(self._only_failed_targets, command),
                    on_output=callback,
                    is_interrupted=self._is_interrupted,
                    on_subprocess_start=_on_subprocess_start,
                    on_subprocess_end=_on_subprocess_end,
                )
                result = pyfltr.command.dispatcher.execute_command(command, self.args, ctx)
            except pyfltr.command.process.InterruptedExecution:
                # execute_command 内部の多段実行経路で Ctrl+C が発生した場合の協調停止。
                self._record_interrupted(command)
                result = pyfltr.command.stage_runner.make_skipped_result(
                    command, self.config, reason="Ctrl+C により中断しました。"
                )
        # ここ以降は結果のUI反映のみなのでserial_groupロックの外で行う。

        with self.lock:
            # running状態から解除
            self._start_times.pop(command, None)

            # Summaryを最終状態に更新
            self._safe_call_from_thread(
                self._update_summary,
                command,
                result.status,
                len(result.errors),
                result.elapsed,
            )

            # JSONパーサー対応ツールはストリーミングしていないため、
            # ErrorLocationベースの表示または生出力フォールバックを出力する。
            if has_custom_parser:
                self._safe_call_from_thread(self._clear_log, f"#output-{command}")
                if result.errors:
                    lines = [pyfltr.output.diagnostics.format_error(e) for e in result.errors]
                    self._safe_call_from_thread(self._write_log, f"#output-{command}", "\n".join(lines))
                    # テスター失敗時は診断一覧に加えて生出力も併記する（text側と同じ設計判断）。
                    # TUIはGitHub Actions注釈記法を使わないためstop-commands保護は不要。
                    if result.command_type == "tester" and result.alerted:
                        self._safe_call_from_thread(self._write_log, f"#output-{command}", result.output)
                elif result.alerted:
                    self._safe_call_from_thread(self._write_log, f"#output-{command}", result.output)
                else:
                    summary = pyfltr.parsing.entry.parse_summary(command, result.output)
                    if summary:
                        self._safe_call_from_thread(self._write_log, f"#output-{command}", summary)
                slow_lines = pyfltr.command.slow_tests.format_slow_tests(result.slow_tests)
                if slow_lines:
                    self._safe_call_from_thread(self._write_log, f"#output-{command}", "\n".join(slow_lines))

            # フッター情報を追記
            footer = f"{'-' * 40}\n終了コード: {result.returncode}\nステータス: {result.get_status_text()}\n"
            self._safe_call_from_thread(
                self._write_log,
                f"#output-{result.command}",
                footer,
            )
            # コマンド失敗時のタブタイトル更新
            if result.failed:
                self._safe_call_from_thread(self._update_tab_title, result.command)

            # エラーまたは警告があればErrorsタブを即時追加/更新
            if result.errors:
                self._all_errors.extend(result.errors)
            sorted_errors = pyfltr.parsing.entry.sort_errors(self._all_errors, self.config.command_names)
            current_warnings = pyfltr.warnings_.collected_warnings()
            if sorted_errors or current_warnings:
                self._safe_call_from_thread(self._update_errors_tab, sorted_errors, current_warnings)

        return result

    async def _update_errors_tab(
        self,
        errors: list[pyfltr.diagnostics.ErrorLocation],
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """Errorsタブを追加または更新。初回のみアクティブに切り替え。

        警告はerrorsの後ろに「warnings:」セクションとして追記する。
        """
        tc = self.query_one(TabbedContent)
        sections: list[str] = [pyfltr.output.diagnostics.format_error(e) for e in errors]
        if warnings:
            if sections:
                sections.append("")
            sections.append("warnings:")
            for entry in warnings:
                sections.append(f"    [{entry['source']}] {entry['message']}")
                sections.append(f"        hint: {entry['hint']}")
        content = "\n".join(sections)

        label = _format_errors_tab_label(len(errors), len(warnings))

        if not self._errors_tab_exists:
            # 初回: タブを追加してアクティブにする
            errors_log = Log(id="errors-log", classes="output")
            errors_pane = TabPane(label, errors_log, id="tab-errors")
            await tc.add_pane(errors_pane, after="summary")
            self._write_log("#errors-log", content)
            self._errors_tab_exists = True
            tc.active = "tab-errors"
        else:
            # 2回目以降: 内容を差し替え（タブ切り替えはしない）
            errors_log = self.query_one("#errors-log", Log)
            errors_log.clear()
            self._write_log("#errors-log", content)
            tab = tc.get_tab("tab-errors")
            # Textualの型スタブはlabelをRenderableType扱いするが、実装はstr代入を許容する。
            tab.label = label  # type: ignore[assignment]

    def _clear_log(self, widget_id: str) -> None:
        """ログをクリアする。"""
        try:
            widget = self.query_one(widget_id, Log)
            widget.clear()
        except Exception:
            logging.error(f"UIエラー: {widget_id}", exc_info=True)

    def _write_log(self, widget_id: str, content: str) -> None:
        """ログの追記。"""
        try:
            widget = self.query_one(widget_id, Log)
            widget.write_lines([("\n" if len(line) == 0 else line) for line in (content + "\n").splitlines(keepends=True)])
        except Exception:
            logging.error(f"UIエラー: {widget_id}", exc_info=True)

    def _update_tab_title(self, command: str) -> None:
        """タブタイトルを更新（エラー時に*を追加）。"""
        try:
            tc = self.query_one(TabbedContent)
            tab = tc.get_tab(f"tab-{command}")
            # Textualの型スタブはlabelをRenderableType扱いするが、実装はstr代入を許容する。
            tab.label = f"{command} *"  # type: ignore[assignment]
        except Exception:
            logging.warning(f"タブタイトル更新失敗: {command}", exc_info=True)

    def _handle_fatal_error(self, msg: str) -> None:
        """致命的エラー時の処理。"""
        logging.error(f"致命的エラーが発生しました: {msg}")
        # アプリケーションを終了
        self.exit(return_code=1)

    def _record_interrupted(self, command: str) -> None:
        """中断されたコマンドを表示用の集合へ記録する。"""
        with self.lock:
            phase = self._phase
            if not isinstance(phase, _Running):
                phase.results.commands[command] = None

    def _show_skipped(self, command: str) -> None:
        """共通実行器が確定したスキップを画面へ反映する。"""
        if self._is_interrupted():
            self._record_interrupted(command)
        self._safe_call_from_thread(self._update_summary, command, "skipped", 0, 0.0)

    def _replace_interrupted_result(
        self,
        result: pyfltr.command.core_.CommandResult,
        stage: str,
    ) -> pyfltr.command.core_.CommandResult:
        """中断時点の実行中集合に従い、表示・保存する結果を差し替える。"""
        phase = self._phase
        if isinstance(phase, _Running):
            return result
        if stage in {"formatter", "parallel"} and (result.command in phase.results.snapshot or result.skipped):
            self._record_interrupted(result.command)
            self._safe_call_from_thread(self._update_summary, result.command, "skipped", 0, 0.0)
            return pyfltr.command.stage_runner.make_skipped_result(
                result.command,
                self.config,
                reason="Ctrl+C により中断しました。",
            )
        return result
