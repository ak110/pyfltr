"""Textual UI関連の処理。"""

import argparse
import concurrent.futures
import logging
import shlex
import sys
import threading
import time
import traceback
import typing

from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Log, TabbedContent, TabPane

import pyfltr.command
import pyfltr.config


def can_use_ui() -> bool:
    """UIを使用するかどうか判定。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_commands_with_ui(commands: list[str], args: argparse.Namespace) -> tuple[list[pyfltr.command.CommandResult], int]:
    """UI付きでコマンドを実行。"""
    app = UIApp(commands, args)
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


class UIApp(App):
    """Textualアプリケーション。"""

    CSS = """
    TabPane {
        height: 1fr;
    }

    .output {
        height: 1fr;
        overflow-y: scroll;
        scrollbar-gutter: stable;
    }
    """

    def __init__(self, commands: list[str], args: argparse.Namespace) -> None:
        super().__init__()
        self.commands = commands
        self.args = args
        self.results: list[pyfltr.command.CommandResult] = []
        self.lock = threading.Lock()
        self.last_ctrl_c_time: float = 0.0
        self.ctrl_c_timeout: float = 1.0  # 1秒以内の連続押しで終了

    def compose(self) -> ComposeResult:
        """UIを構成。"""
        with TabbedContent(initial="summary"):
            with TabPane("Summary", id="summary"):
                yield VerticalScroll(Log(id="summary-content"), classes="output")

            # 有効なコマンドのみタブを作成
            enabled_commands = [cmd for cmd in self.commands if pyfltr.config.CONFIG[cmd]]
            for command in enabled_commands:
                with TabPane(command, id=f"tab-{command}"):
                    yield VerticalScroll(Log(id=f"output-{command}"), classes="output")

    def on_ready(self) -> None:
        """mount時の処理。"""
        # 初期表示
        self._write_log("#summary-content", "Running commands... (Press Ctrl+C twice to exit)\n\n")
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
                self._write_log("#summary-content", "Press Ctrl+C again within 1 second to exit...\n")

    def _run_commands(self) -> None:
        """backgroundでコマンドを実行。"""
        threading.Thread(target=self._run_in_background, daemon=True).start()

    def _run_in_background(self):
        """バックグラウンド処理。"""
        try:
            # formatters (serial)
            for command in self.commands:
                if pyfltr.config.CONFIG[command] and pyfltr.config.ALL_COMMANDS[command].type == "formatter":
                    self.results.append(self._execute_command(command))

            # linters/testers (parallel)
            linter_commands = []
            for command in self.commands:
                if pyfltr.config.CONFIG[command] and pyfltr.config.ALL_COMMANDS[command].type != "formatter":
                    linter_commands.append(command)

            if len(linter_commands) > 0:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(linter_commands)) as executor:
                    future_to_command = {
                        executor.submit(self._execute_command, command): command for command in linter_commands
                    }
                    for future in concurrent.futures.as_completed(future_to_command):
                        self.results.append(future.result())

            # summary更新と自動終了判定
            summary_lines = ["", "", "Results summary:", "=" * 40]
            for result in self.results:
                summary_lines.append(f"{result.command:<16s} {result.get_status_text()}")

            summary_lines.extend(["=" * 40])

            # returncode情報と自動終了判定
            statuses = [result.status for result in self.results]
            overall_status: typing.Literal["SUCCESS", "FORMATTED", "FAILED"]
            if any(status == "failed" for status in statuses):
                overall_status = "FAILED"
            elif any(status == "formatted" for status in statuses):
                overall_status = "FORMATTED"
            else:
                overall_status = "SUCCESS"

            summary_lines.append(f"Overall status: {overall_status}")
            summary_lines.append("")
            summary_lines.append("")
            self.call_from_thread(self._write_log, "#summary-content", "\n".join(summary_lines))

            # FORMATTED/SUCCESSの場合は自動終了
            if overall_status != "FAILED":
                self.call_from_thread(self.exit)
                # self.call_from_thread(self.set_timer, 1, self.exit)

        except Exception:
            # Textualエラー時の処理
            error_msg = f"Fatal error in UI processing:\n{traceback.format_exc()}"
            try:
                # summaryタブにエラー表示
                self.call_from_thread(
                    self._write_log,
                    "#summary-content",
                    f"FATAL ERROR:\n{error_msg}\n\nPress Ctrl+C to exit.",
                )
            except Exception:
                logging.error(error_msg)
                self.call_from_thread(self._handle_fatal_error, error_msg)

    def _execute_command(self, command: str) -> pyfltr.command.CommandResult:
        """outputをキャプチャしながらコマンド実行。"""
        # コマンドタブに開始メッセージを出力
        self.call_from_thread(
            self._write_log,
            f"#output-{command}",
            f"Running {command}...\n",
        )

        result = pyfltr.command.execute_command(command, self.args)

        with self.lock:
            # コマンド実行が完了した旨をサマリタブに出力
            self.call_from_thread(
                self._write_log,
                "#summary-content",
                f"Command {command} completed. ({result.status})\n",
            )

            # コマンド実行結果をコマンドタブに出力
            command_tab_output = (
                f"Command: {shlex.join(result.commandline)}\n"
                f"{'-' * 40}\n"
                f"{result.output.rstrip()}\n"
                f"{'-' * 40}\n"
                f"Return code: {result.returncode}\n"
                f"Status: {result.get_status_text()}\n"
            )
            self.call_from_thread(
                self._write_log,
                f"#output-{result.command}",
                command_tab_output,
            )
            # コマンド失敗時のタブタイトル更新
            if result.status == "failed":
                self.call_from_thread(self._update_tab_title, result.command, True)

        return result

    def _write_log(self, widget_id: str, content: str) -> None:
        """ログの追記。"""
        try:
            widget = self.query_one(widget_id, Log)
            widget.write(content)
            # 強制的に画面を更新
            self.refresh()
        except Exception:
            logging.error(f"UIエラー: {widget_id}", exc_info=True)

    def _update_tab_title(self, command: str, has_error: bool) -> None:
        """タブタイトルを更新（エラー時に*を追加）。"""
        # pylint: disable=protected-access
        try:
            tc = self.query_one(TabbedContent)
            tab = tc.get_tab(f"tab-{command}")
            if has_error:
                tab.label = f"{command} *"  # type: ignore[assignment]
                # エラーが発生したタブをアクティブにする
                tc.active = f"tab-{command}"
            else:
                tab.label = command  # type: ignore[assignment]
        except Exception:
            logging.warning(f"タブタイトル更新失敗: {command}", exc_info=True)

    def _handle_fatal_error(self, msg: str) -> None:
        """致命的エラー時の処理。"""
        logging.error(f"Fatal error occurred: {msg}")
        # アプリケーションを終了
        self.exit(return_code=1)
