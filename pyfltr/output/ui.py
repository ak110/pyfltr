"""Textual UI関連の処理。"""

import argparse
import concurrent.futures
import contextlib
import logging
import sys
import threading
import time
import typing

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Log, TabbedContent, TabPane

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.error_parser
import pyfltr.command.process
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.state.executor
import pyfltr.state.only_failed
import pyfltr.state.stage_runner
import pyfltr.warnings_


def can_use_ui() -> bool:
    """UIを使用するかどうか判定。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _format_errors_tab_label(error_count: int, warning_count: int) -> str:
    """Errors タブのラベル文字列を組み立てる。"""
    if warning_count:
        return f"Errors ({error_count}/{warning_count}w)"
    return f"Errors ({error_count})"


def run_commands_with_ui(
    commands: list[str],
    args: argparse.Namespace,
    base_ctx: pyfltr.command.core_.ExecutionBaseContext,
    *,
    archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    on_result: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
    fail_fast: bool = False,
    only_failed_targets: dict[str, pyfltr.state.only_failed.ToolTargets] | None = None,
) -> tuple[list[pyfltr.command.core_.CommandResult], int]:
    """UI付きでコマンドを実行。

    `base_ctx`はパイプライン全体で不変のコンテキスト（config・all_files・cache_store・
    cache_run_idを含む）。各コマンド実行前に`ExecutionContext`を組み立てて渡す。

    `archive_hook`が指定されている場合、各コマンド完了時に実行アーカイブへ書き込む
    （fixステージも含めて全実行を保存する）。キャッシュヒット時の結果はアーカイブには
    書き込まない（`cached_from`でソースrunを参照させる前提）。

    `on_result`が指定されている場合、通常ステージ（formatter / linter / tester）の
    各コマンド完了時にarchive_hookの後に呼ぶ。fixステージでは呼ばない。

    `fail_fast=True`のとき、いずれかのツールが`has_error=True`で完了した時点で
    未実行ジョブを`future.cancel()`で打ち切り、起動済みサブプロセスに
    `terminate()`を送る。

    `only_failed_targets`が指定された場合、ツール別の失敗ファイル集合を
    `execute_command`へ渡す（`--only-failed`経路）。値が`None`のツールは通常の
    `all_files`で実行し、`list`のツールはその集合のみを対象にする。
    """
    app = UIApp(
        commands,
        args,
        base_ctx,
        archive_hook=archive_hook,
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
        print(f"エラー: UI アプリケーションの実行に失敗しました: {exc}", file=sys.stderr)
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
        args: argparse.Namespace,
        base_ctx: pyfltr.command.core_.ExecutionBaseContext,
        *,
        archive_hook: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
        on_result: typing.Callable[[pyfltr.command.core_.CommandResult], None] | None = None,
        fail_fast: bool = False,
        only_failed_targets: dict[str, pyfltr.state.only_failed.ToolTargets] | None = None,
    ) -> None:
        super().__init__()
        self.commands = commands
        self.args = args
        self._base_ctx = base_ctx
        # base_ctxの頻出フィールドをショートカットとして保持する
        self.config = base_ctx.config
        self._all_files = base_ctx.all_files
        self._archive_hook = archive_hook
        self._on_result = on_result
        self._fail_fast = fail_fast
        self._only_failed_targets = only_failed_targets
        self.results: list[pyfltr.command.core_.CommandResult] = []
        self.lock = threading.Lock()
        self.last_ctrl_c_time: float = 0.0
        self.ctrl_c_timeout: float = 1.0  # 1秒以内の連続押しで終了
        # 各コマンドの開始時刻（running中の経過時間表示用）
        self._start_times: dict[str, float] = {}
        # 協調中断用の状態（いずれも`self.lock`下で読み書きする）。
        # `_subprocess_running_commands`はsubprocessが実際に動いている区間を追跡する集合。
        # UI反映の残り時間まで「実行中」に含めると、Ctrl+C直後のスナップショットに完了済み
        # コマンドが混入してしまうため`_start_times`ではなく専用集合を使う。
        # `_interrupt_running_snapshot`はCtrl+C受信時点の実行中集合のコピーで、
        # linter/tester段のループで「中断された」と判定すべき対象を限定するのに使う。
        # `_interrupted_commands`は中断対象のコマンド名を登録順に保持する（warnings出力用）。
        self._interrupted: bool = False
        # `_exit_requested`はUIイベントループ終了を要求済みか（TUI強制終了経路・BG最終段）を示す。
        # 設定された後にworkerスレッドが`_safe_call_from_thread`を呼ぶと、閉じつつあるイベントループ
        # への`call_from_thread`が滞留する可能性があるため、短絡する。
        self._exit_requested: bool = False
        self._subprocess_running_commands: set[str] = set()
        self._interrupt_running_snapshot: set[str] = set()
        self._interrupted_commands: dict[str, None] = {}
        # エラー蓄積用（Errorsタブの即時更新に使用）
        self._all_errors: list[pyfltr.command.error_parser.ErrorLocation] = []
        self._errors_tab_exists = False

    def compose(self) -> ComposeResult:
        """UIを構成。"""
        with TabbedContent(initial="summary"):
            with TabPane("Summary", id="summary"):
                yield DataTable(id="summary-table")

            # 有効なコマンドのみタブを作成
            # (Errorsタブはエラー発生時にsummaryの直後に動的追加)
            enabled_commands = [cmd for cmd in self.commands if self.config[cmd]]
            for command in enabled_commands:
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

        enabled_commands = [cmd for cmd in self.commands if self.config[cmd]]
        for command in enabled_commands:
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

    def on_key(self, event) -> None:
        """キー入力処理。

        Ctrl+C×2で協調停止を開始する。`self.exit()`を即時呼ばずに`_interrupted`
        フラグを立てて`terminate_active_processes()`でサブプロセスのみ止め、
        BGスレッドが完了済み結果を`self.results`に書き込み終えるのを待つ。
        BGスレッドが長引いた場合の退路として、既に中断済みの状態でさらにCtrl+C×2を
        到来したら`self.exit(return_code=130)`で強制終了する。
        """
        if event.key == "ctrl+c":
            current_time = time.time()
            within_window = current_time - self.last_ctrl_c_time <= self.ctrl_c_timeout
            if within_window:
                if not self._interrupted:
                    # 1回目のCtrl+C×2: 協調中断を開始する。
                    with self.lock:
                        self._interrupt_running_snapshot = set(self._subprocess_running_commands)
                        self._interrupted = True
                    pyfltr.command.process.terminate_active_processes()
                    self.notify("中断処理中です。完了済みツールの結果をまとめています...")
                    self.last_ctrl_c_time = current_time
                else:
                    # 協調中断済みの状態でさらにCtrl+C×2: 強制終了。
                    # 以降のBGスレッド → UI反映経路は短絡させ、サブプロセス停止を
                    # もう一度念押ししてからexitする（孫プロセスが残ってworkerが
                    # 終わらないケースの退路）。
                    self._exit_requested = True
                    pyfltr.command.process.terminate_active_processes()
                    self.exit(return_code=130)
            else:
                # 初回またはタイムアウト後のCtrl+C
                self.last_ctrl_c_time = current_time
                # ユーザーに2回目を促すメッセージを表示
                self.notify("終了するには 1 秒以内にもう一度 Ctrl+C を押してください。")

    def _safe_call_from_thread(self, callback, *args, **kwargs) -> None:
        """イベントループ喪失時に抑止する`call_from_thread`ラッパー。

        協調停止ではUIイベントループが閉じた後もBGスレッドが短時間実行し続ける設計だが、
        Textualはイベントループ喪失時に例外を送出する。BGスレッドはここで結果を
        `self.results`に蓄積する責務があるため、UI反映側の失敗でBGスレッドを
        停止させないようにする。

        `_exit_requested=True`のときはUI反映を完全に短絡する。閉じつつあるイベントループへ
        `call_from_thread`すると`run_coroutine_threadsafe(...).result()`で長引き、
        workerスレッドが`ThreadPoolExecutor`の`shutdown(wait=True)`にたどり着けなくなる
        病理を避けるため。
        """
        if self._exit_requested:
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
            include_fix_stage = bool(getattr(self.args, "include_fix_stage", False))
            fixers, formatters, linters_and_testers = pyfltr.state.executor.split_commands_for_execution(
                self.commands, self.config, self._all_files, include_fix_stage=include_fix_stage
            )
            aborted = False

            # fixステージ（serial）。結果はsummaryに含めず、後段の通常ステージに委ねる。
            # アーカイブにはfixステージも含めて全実行を保存する。
            for command in fixers:
                fix_result = self._execute_command(command, fix_stage=True)
                # 中断検知。fix結果はsummary対象外なのでresultsには追加しない。
                # アーカイブへの記録は途中中断の診断情報として有用なので通常のフック条件を維持する。
                if self._archive_hook is not None and not fix_result.cached:
                    self._archive_hook(fix_result)
                if self._interrupted:
                    with self.lock:
                        self._interrupted_commands[command] = None
                    # 現在のfixコマンドも通常ステージで同名skippedとして再登録する
                    # （summaryは通常ステージ側にだけ出る）。
                    self._skip_remaining(
                        [*formatters, *linters_and_testers],
                        reason="Ctrl+C により中断しました。",
                        register_interrupted=True,
                    )
                    aborted = True
                    break
                if self._fail_fast and fix_result.has_error:
                    aborted = True
                    self._skip_remaining([*formatters, *linters_and_testers])
                    break

            # formatters（serial）
            if not aborted:
                for idx, command in enumerate(formatters):
                    fmt_result = self._execute_command(command)
                    if self._interrupted:
                        # 当該formatter結果自体が非skippedならskippedに置き換える。
                        if fmt_result.status != "skipped":
                            fmt_result = pyfltr.state.stage_runner.make_skipped_result(
                                command, self.config, reason="Ctrl+C により中断しました。"
                            )
                        self.results.append(fmt_result)
                        if self._archive_hook is not None and not fmt_result.cached:
                            self._archive_hook(fmt_result)
                        if self._on_result is not None:
                            self._on_result(fmt_result)
                        with self.lock:
                            self._interrupted_commands[command] = None
                        self._skip_remaining(
                            [*formatters[idx + 1 :], *linters_and_testers],
                            reason="Ctrl+C により中断しました。",
                            register_interrupted=True,
                        )
                        aborted = True
                        break
                    self.results.append(fmt_result)
                    if self._archive_hook is not None and not fmt_result.cached:
                        self._archive_hook(fmt_result)
                    if self._on_result is not None:
                        self._on_result(fmt_result)
                    if self._fail_fast and fmt_result.has_error:
                        aborted = True
                        self._skip_remaining([*formatters[idx + 1 :], *linters_and_testers])
                        break

            # linters/testers（parallel）
            if not aborted and len(linters_and_testers) > 0:
                aborted_commands: set[str] = set()
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.config["jobs"]) as executor:
                    future_to_command = {
                        executor.submit(self._execute_command, command): command for command in linters_and_testers
                    }
                    for future in concurrent.futures.as_completed(future_to_command):
                        command = future_to_command[future]
                        try:
                            lt_result = future.result()
                        except concurrent.futures.CancelledError:
                            aborted_commands.add(command)
                            continue
                        # 中断時に実行中だったコマンド、またはskippedで返ってきたコマンドは
                        # まとめて「Ctrl+Cにより中断しました。」扱いに揃える。完了済み結果は
                        # そのまま残してsummaryに反映する（中断でも進捗を確認できるようにするため）。
                        if self._interrupted and (command in self._interrupt_running_snapshot or lt_result.status == "skipped"):
                            lt_result = pyfltr.state.stage_runner.make_skipped_result(
                                command, self.config, reason="Ctrl+C により中断しました。"
                            )
                            with self.lock:
                                self._interrupted_commands[command] = None
                            self._safe_call_from_thread(self._update_summary, command, "skipped", 0, 0.0)
                        self.results.append(lt_result)
                        if self._archive_hook is not None and not lt_result.cached:
                            self._archive_hook(lt_result)
                        if self._on_result is not None:
                            self._on_result(lt_result)
                        if self._fail_fast and not aborted and lt_result.has_error:
                            aborted = True
                            pyfltr.state.stage_runner.cancel_pending_futures(future_to_command, aborted_commands)
                            pyfltr.command.process.terminate_active_processes()
                    # 中断済みの場合は未開始futureをまとめてキャンセルする（終端処理）。
                    if self._interrupted:
                        pyfltr.state.stage_runner.cancel_pending_futures(future_to_command, aborted_commands)
                if aborted_commands:
                    reason = "Ctrl+C により中断しました。" if self._interrupted else None
                    for pending_command in aborted_commands:
                        skipped = pyfltr.state.stage_runner.make_skipped_result(pending_command, self.config, reason=reason)
                        self.results.append(skipped)
                        if self._archive_hook is not None:
                            self._archive_hook(skipped)
                        if self._on_result is not None:
                            self._on_result(skipped)
                        if self._interrupted:
                            with self.lock:
                                self._interrupted_commands[pending_command] = None
                        self._safe_call_from_thread(
                            self._update_summary,
                            pending_command,
                            "skipped",
                            0,
                            0.0,
                        )

            # 中断時はwarnings欄に中断通知を1行出力する。
            if self._interrupted:
                with self.lock:
                    interrupted_ordered = list(self._interrupted_commands)
                # config.command_names順に並べ替えて一意化（UI定義順）。
                index_map = {name: i for i, name in enumerate(self.config.command_names)}
                interrupted_sorted = sorted(interrupted_ordered, key=lambda c: index_map.get(c, len(index_map)))
                if interrupted_sorted:
                    names = ", ".join(interrupted_sorted)
                    pyfltr.warnings_.emit_warning(
                        source="pyfltr",
                        message=f"Ctrl+C により中断しました。中断されたツール: {names}",
                    )
                else:
                    pyfltr.warnings_.emit_warning(
                        source="pyfltr",
                        message="Ctrl+C により中断しました。",
                    )
                # 協調中断の最終段。自身のexitは確実にUIへ伝えたうえで
                # `_exit_requested`を設定し、以降のworker側`_safe_call_from_thread`を短絡させる。
                try:
                    self.call_from_thread(self.exit, return_code=130)
                except Exception:
                    logging.getLogger(__name__).debug(
                        "call_from_thread(self.exit) 失敗（イベントループ喪失の可能性）",
                        exc_info=True,
                    )
                self._exit_requested = True
                return

            # 自動終了判定
            statuses = [result.status for result in self.results]
            overall_status: typing.Literal["SUCCESS", "FORMATTED", "FAILED"]
            if any(status in {"failed", "resolution_failed"} for status in statuses):
                overall_status = "FAILED"
            elif any(status == "formatted" for status in statuses):
                overall_status = "FORMATTED"
            else:
                overall_status = "SUCCESS"

            # FORMATTED/SUCCESSの場合は自動終了（--keep-ui時は終了しない）
            if overall_status != "FAILED" and not self.args.keep_ui:
                self._safe_call_from_thread(self.exit)

        except Exception as exc:
            # Textualエラー時の処理。詳細はlogger（exc_info=True）に記録し、画面側は1行サマリのみ。
            error_msg = f"UI 処理中に致命的エラーが発生しました: {exc}"
            logging.error("UI 処理中に致命的エラーが発生しました: %s", exc, exc_info=True)
            # call_from_thread自体が失敗した場合は既にlogging済みのため追加処理不要。
            with contextlib.suppress(Exception):
                self.call_from_thread(self._handle_fatal_error, error_msg)

    def _execute_command(self, command: str, *, fix_stage: bool = False) -> pyfltr.command.core_.CommandResult:
        """出力をキャプチャしながらコマンド実行。"""
        # 中断済みならsubprocessを一切起動せずskippedで返す（早期離脱）。
        if self._interrupted:
            with self.lock:
                self._interrupted_commands[command] = None
            return pyfltr.state.stage_runner.make_skipped_result(command, self.config, reason="Ctrl+C により中断しました。")

        # serial_groupを持つコマンドは同一グループ内で排他実行される（cargo / dotnet等）。
        # ロック取得前は「待機中」の表示に留め、running表示はロック取得後に切り替える。
        with pyfltr.state.executor.serial_group_lock(self.config.commands[command].serial_group):
            # ロック取得後の再チェック。serial_group待機中にCtrl+Cを受け取った場合、
            # ロック取得後にsubprocessを起動せずskippedで返すことで協調停止前提を保つ。
            if self._interrupted:
                with self.lock:
                    self._interrupted_commands[command] = None
                return pyfltr.state.stage_runner.make_skipped_result(command, self.config, reason="Ctrl+C により中断しました。")

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
            has_custom_parser = command in pyfltr.command.error_parser.get_custom_parser_commands()
            callback: typing.Callable[[str], None] | None = None
            if not has_custom_parser:

                def _on_output(line: str) -> None:
                    """出力行をリアルタイムでUIに反映。"""
                    self._safe_call_from_thread(self._write_log, f"#output-{command}", line.removesuffix("\n"))

                callback = _on_output

            def _on_subprocess_start() -> None:
                with self.lock:
                    self._subprocess_running_commands.add(command)

            def _on_subprocess_end() -> None:
                with self.lock:
                    self._subprocess_running_commands.discard(command)

            try:
                ctx = pyfltr.command.core_.ExecutionContext(
                    base=self._base_ctx,
                    fix_stage=fix_stage,
                    only_failed_targets=pyfltr.command.targets.pick_targets(self._only_failed_targets, command),
                    on_output=callback,
                    is_interrupted=lambda: self._interrupted,
                    on_subprocess_start=_on_subprocess_start,
                    on_subprocess_end=_on_subprocess_end,
                )
                result = pyfltr.command.dispatcher.execute_command(command, self.args, ctx)
            except pyfltr.command.process.InterruptedExecution:
                # execute_command 内部の多段実行経路で Ctrl+C が発生した場合の協調停止。
                with self.lock:
                    self._interrupted_commands[command] = None
                result = pyfltr.state.stage_runner.make_skipped_result(
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
                    lines = [pyfltr.command.error_parser.format_error(e) for e in result.errors]
                    self._safe_call_from_thread(self._write_log, f"#output-{command}", "\n".join(lines))
                elif result.alerted:
                    self._safe_call_from_thread(self._write_log, f"#output-{command}", result.output)
                else:
                    summary = pyfltr.command.error_parser.parse_summary(command, result.output)
                    if summary:
                        self._safe_call_from_thread(self._write_log, f"#output-{command}", summary)

            # フッター情報を追記
            footer = f"{'-' * 40}\n終了コード: {result.returncode}\nステータス: {result.get_status_text()}\n"
            self._safe_call_from_thread(
                self._write_log,
                f"#output-{result.command}",
                footer,
            )
            # コマンド失敗時のタブタイトル更新
            if result.status in {"failed", "resolution_failed"}:
                self._safe_call_from_thread(self._update_tab_title, result.command)

            # エラーまたは警告があればErrorsタブを即時追加/更新
            if result.errors:
                self._all_errors.extend(result.errors)
            sorted_errors = pyfltr.command.error_parser.sort_errors(self._all_errors, self.config.command_names)
            current_warnings = pyfltr.warnings_.collected_warnings()
            if sorted_errors or current_warnings:
                self._safe_call_from_thread(self._update_errors_tab, sorted_errors, current_warnings)

        return result

    async def _update_errors_tab(
        self,
        errors: list[pyfltr.command.error_parser.ErrorLocation],
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """Errorsタブを追加または更新。初回のみアクティブに切り替え。

        警告はerrorsの後ろに「warnings:」セクションとして追記する。
        """
        tc = self.query_one(TabbedContent)
        sections: list[str] = [pyfltr.command.error_parser.format_error(e) for e in errors]
        if warnings:
            if sections:
                sections.append("")
            sections.append("warnings:")
            for entry in warnings:
                sections.append(f"    [{entry['source']}] {entry['message']}")
                hint = entry.get("hint")
                if hint is not None:
                    sections.append(f"        hint: {hint}")
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

    def _skip_remaining(
        self,
        commands: list[str],
        *,
        reason: str | None = None,
        register_interrupted: bool = False,
    ) -> None:
        """中断経路で未実行ツールをskippedとして登録する（fix/formatter段から）。

        `reason`は`make_skipped_result`へそのまま渡す（省略時は--fail-fastの既定文言）。
        `register_interrupted=True`のとき、各コマンド名を`self._interrupted_commands`にも
        登録する（Ctrl+C経路限定。fail-fastでは登録しない）。
        """
        pyfltr.command.process.terminate_active_processes()
        for command in commands:
            skipped = pyfltr.state.stage_runner.make_skipped_result(command, self.config, reason=reason)
            self.results.append(skipped)
            if self._archive_hook is not None:
                self._archive_hook(skipped)
            if self._on_result is not None:
                self._on_result(skipped)
            if register_interrupted:
                with self.lock:
                    self._interrupted_commands[command] = None
            self._safe_call_from_thread(self._update_summary, command, "skipped", 0, 0.0)
