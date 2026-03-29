"""Textual UI関連の処理。"""

import argparse
import concurrent.futures
import logging
import sys
import threading
import time
import traceback
import typing

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Log, TabbedContent, TabPane

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.executor


def can_use_ui() -> bool:
    """UIを使用するかどうか判定。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_commands_with_ui(
    commands: list[str],
    args: argparse.Namespace,
    config: pyfltr.config.Config,
) -> tuple[list[pyfltr.command.CommandResult], int]:
    """UI付きでコマンドを実行。"""
    app = UIApp(commands, args, config)
    try:
        return_code = app.run()
        if return_code is None:
            return_code = 0
        else:
            assert isinstance(return_code, int)

        return app.results, return_code
    except Exception:
        # Textualアプリケーション自体の例外処理
        error_msg = f"Failed to run UI application: {traceback.format_exc()}"
        logging.error(error_msg)
        print(f"ERROR: {error_msg}", file=sys.stderr)
        sys.exit(1)


# ステータス表示用の定義
_STATUS_DISPLAY: dict[str, str] = {
    "waiting": "○ waiting",
    "running": "● running",
    "succeeded": "✓ done",
    "failed": "⚠ failed",
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

    def __init__(self, commands: list[str], args: argparse.Namespace, config: pyfltr.config.Config) -> None:
        super().__init__()
        self.commands = commands
        self.args = args
        self.config = config
        self.results: list[pyfltr.command.CommandResult] = []
        self.lock = threading.Lock()
        self.last_ctrl_c_time: float = 0.0
        self.ctrl_c_timeout: float = 1.0  # 1秒以内の連続押しで終了
        # 各コマンドの開始時刻（running中の経過時間表示用）
        self._start_times: dict[str, float] = {}
        # エラー蓄積用（Errorsタブの即時更新に使用）
        self._all_errors: list[pyfltr.error_parser.ErrorLocation] = []
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
        """キー入力処理。"""
        if event.key == "ctrl+c":
            current_time = time.time()

            # 前回のCtrl+Cから1秒以内の場合は終了
            if current_time - self.last_ctrl_c_time <= self.ctrl_c_timeout:
                self.exit()  # return_code=130 : 128+SIGINT(2) もありだが…
            else:
                # 初回またはタイムアウト後のCtrl+C
                self.last_ctrl_c_time = current_time
                # ユーザーに2回目を促すメッセージを表示
                self.notify("Press Ctrl+C again within 1 second to exit...")

    def _update_elapsed_times(self) -> None:
        """running中のコマンドの経過時間を更新。"""
        table = self.query_one("#summary-table", DataTable)
        for command, start_time in self._start_times.items():
            elapsed = time.perf_counter() - start_time
            table.update_cell(command, "time", f"{elapsed:.0f}s…")

    def _update_summary(self, command: str, status: str, error_count: int | None = None, elapsed: float | None = None) -> None:
        """Summaryテーブルの行を更新。"""
        table = self.query_one("#summary-table", DataTable)
        table.update_cell(command, "status", _STATUS_DISPLAY.get(status, status))
        if error_count is not None:
            table.update_cell(command, "errors", str(error_count))
        if elapsed is not None:
            table.update_cell(command, "time", f"{elapsed:.1f}s")

    def _run_commands(self) -> None:
        """backgroundでコマンドを実行。"""
        threading.Thread(target=self._run_in_background, daemon=True).start()

    def _run_in_background(self):
        """バックグラウンド処理。"""
        try:
            formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(self.commands, self.config)

            # formatters (serial)
            for command in formatters:
                self.results.append(self._execute_command(command))

            # linters/testers (parallel)
            if len(linters_and_testers) > 0:
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.config["jobs"]) as executor:
                    future_to_command = {
                        executor.submit(self._execute_command, command): command for command in linters_and_testers
                    }
                    for future in concurrent.futures.as_completed(future_to_command):
                        self.results.append(future.result())

            # 自動終了判定
            statuses = [result.status for result in self.results]
            overall_status: typing.Literal["SUCCESS", "FORMATTED", "FAILED"]
            if any(status == "failed" for status in statuses):
                overall_status = "FAILED"
            elif any(status == "formatted" for status in statuses):
                overall_status = "FORMATTED"
            else:
                overall_status = "SUCCESS"

            # FORMATTED/SUCCESSの場合は自動終了（--keep-ui時は終了しない）
            if overall_status != "FAILED" and not self.args.keep_ui:
                self.call_from_thread(self.exit)

        except Exception:
            # Textualエラー時の処理
            error_msg = f"Fatal error in UI processing:\n{traceback.format_exc()}"
            try:
                self.call_from_thread(self._handle_fatal_error, error_msg)
            except Exception:
                logging.error(error_msg)

    def _execute_command(self, command: str) -> pyfltr.command.CommandResult:
        """outputをキャプチャしながらコマンド実行。"""
        # Summaryを「running」に更新
        self._start_times[command] = time.perf_counter()
        self.call_from_thread(self._update_summary, command, "running")

        # コマンドタブに開始メッセージを出力
        self.call_from_thread(
            self._write_log,
            f"#output-{command}",
            f"Running {command}...\n",
        )

        def on_output(line: str) -> None:
            """出力行をリアルタイムでUIに反映。"""
            self.call_from_thread(self._write_log, f"#output-{command}", line.removesuffix("\n"))

        result = pyfltr.command.execute_command(command, self.args, self.config, on_output=on_output)

        with self.lock:
            # running状態から解除
            self._start_times.pop(command, None)

            # Summaryを最終状態に更新
            self.call_from_thread(
                self._update_summary,
                command,
                result.status,
                len(result.errors),
                result.elapsed,
            )

            # フッター情報のみ追記（本体はストリーミング済み）
            footer = f"{'-' * 40}\nReturn code: {result.returncode}\nStatus: {result.get_status_text()}\n"
            self.call_from_thread(
                self._write_log,
                f"#output-{result.command}",
                footer,
            )
            # コマンド失敗時のタブタイトル更新
            if result.status == "failed":
                self.call_from_thread(self._update_tab_title, result.command)

            # エラーがあればErrorsタブを即時追加/更新
            if result.errors:
                self._all_errors.extend(result.errors)
                sorted_errors = pyfltr.error_parser.sort_errors(self._all_errors, self.config.command_names)
                self.call_from_thread(self._update_errors_tab, sorted_errors)  # type: ignore[arg-type]

        return result

    async def _update_errors_tab(self, errors: list[pyfltr.error_parser.ErrorLocation]) -> None:
        """Errorsタブを追加または更新。初回のみアクティブに切り替え。"""
        tc = self.query_one(TabbedContent)
        lines = [pyfltr.error_parser.format_error(e) for e in errors]
        content = "\n".join(lines)

        if not self._errors_tab_exists:
            # 初回: タブを追加してアクティブにする
            errors_log = Log(id="errors-log", classes="output")
            errors_pane = TabPane(f"Errors ({len(errors)})", errors_log, id="tab-errors")
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
            tab.label = f"Errors ({len(errors)})"  # type: ignore[assignment]

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
            tab.label = f"{command} *"  # type: ignore[assignment]
        except Exception:
            logging.warning(f"タブタイトル更新失敗: {command}", exc_info=True)

    def _handle_fatal_error(self, msg: str) -> None:
        """致命的エラー時の処理。"""
        logging.error(f"Fatal error occurred: {msg}")
        # アプリケーションを終了
        self.exit(return_code=1)
