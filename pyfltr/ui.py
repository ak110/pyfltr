"""Textual UI関連の処理。"""

import argparse
import logging
import shlex
import sys
import threading
import traceback
import typing

import joblib
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Log, TabbedContent, TabPane

import pyfltr.command
import pyfltr.config


def can_use_ui() -> bool:
    """UIを使用するかどうか判定。"""
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_commands_with_ui(commands: list[str], args: argparse.Namespace) -> list[pyfltr.command.CommandResult]:
    """UI付きでコマンドを実行。"""
    app = PyfltrApp(commands, args)
    try:
        app.run()

        # 致命的エラーがあった場合は異常終了
        if app.fatal_error:
            logging.error(f"Fatal error occurred: {app.fatal_error}")
            sys.exit(1)

        return app.results
    except Exception:
        # Textualアプリケーション自体の例外処理
        error_msg = f"Failed to run UI application: {traceback.format_exc()}"
        logging.error(error_msg)
        print(f"ERROR: {error_msg}", file=sys.stderr)
        sys.exit(1)


class PyfltrApp(App):
    """Pyfltr Textualアプリケーション。"""

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
        self.command_status: dict[str, str] = {}
        self.fatal_error: str | None = None

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
        self._update_widget("#summary-content", "Running commands... (Press Ctrl+C to exit)\n\n")
        # コマンド実行をバックグラウンドで開始
        self.set_timer(0.1, self._run_commands)

    def on_key(self, event) -> None:
        """キー入力処理。"""
        if event.key == "ctrl+c":
            self.exit()

    def _run_commands(self) -> None:
        """backgroundでコマンドを実行。"""
        threading.Thread(target=self._run_in_background, daemon=True).start()

    def _run_in_background(self):
        """バックグラウンド処理。"""
        try:
            # formatters (serial)
            for command in self.commands:
                if pyfltr.config.CONFIG[command] and pyfltr.config.ALL_COMMANDS[command]["type"] == "formatter":
                    self.call_from_thread(
                        self._update_widget,
                        f"#output-{command}",
                        f"Running {command}...\n\n",
                    )
                    result = self._execute_command(command)
                    self.results.append(result)

                    status_lines = (
                        f"Command: {shlex.join(result.commandline)}\n"
                        f"Return code: {result.returncode}\n"
                        f"Status: {result.status} "
                        f"({result.files} files in {result.elapsed:.1f}s)\n"
                        f"{'-' * 40}\n\n"
                    )
                    self.call_from_thread(self._update_widget, f"#output-{command}", status_lines + result.output)

                    # コマンド失敗時のタブタイトル更新
                    if result.status == "failed":
                        self.call_from_thread(self._update_tab_title, command, True)

            # linters/testers (parallel)
            jobs: list[typing.Any] = []
            for command in self.commands:
                if pyfltr.config.CONFIG[command] and pyfltr.config.ALL_COMMANDS[command]["type"] != "formatter":
                    self.call_from_thread(
                        self._update_widget,
                        f"#output-{command}",
                        f"Running {command}...\n",
                    )
                    jobs.append(joblib.delayed(self._execute_command)(command))

            if len(jobs) > 0:
                with joblib.Parallel(n_jobs=len(jobs), backend="threading") as parallel:
                    parallel_results = parallel(jobs)
                    self.results.extend(parallel_results)
                    for result in parallel_results:
                        status_lines = (
                            f"Command: {shlex.join(result.commandline)}\n"
                            f"Return code: {result.returncode}\n"
                            f"Status: {result.status} "
                            f"({result.files} files in {result.elapsed:.1f}s)\n"
                            f"{'-' * 40}\n\n"
                        )
                        self.call_from_thread(
                            self._update_widget,
                            f"#output-{result.command}",
                            status_lines + result.output,
                        )

                        # コマンド失敗時のタブタイトル更新
                        if result.status == "failed":
                            self.call_from_thread(self._update_tab_title, result.command, True)

            # summary更新
            self._update_summary()

        except Exception:
            # Textualエラー時の処理
            error_msg = f"Fatal error in UI processing:\n{traceback.format_exc()}"
            try:
                # summaryタブにエラー表示
                self.call_from_thread(
                    self._update_widget,
                    "#summary-content",
                    f"FATAL ERROR:\n{error_msg}\n\nPress Ctrl+C to exit.",
                )
            except Exception:
                self.fatal_error = error_msg
                logging.error(error_msg)
                self.call_from_thread(self._handle_fatal_error)

    def _execute_command(self, command: str) -> pyfltr.command.CommandResult:
        """outputをキャプチャしながらコマンド実行。"""
        result = pyfltr.command.execute_command(command, self.args)
        return result

    def _update_summary(self) -> None:
        """summary更新。"""
        summary_lines = ["Command Summary:", "=" * 40]
        for result in self.results:
            summary_lines.append(f"{result.command:<16s} {result.get_status_text()}")

        summary_lines.extend(["=" * 40])

        # returncode情報
        statuses = [result.status for result in self.results]
        if any(status == "failed" for status in statuses):
            summary_lines.append("Overall status: FAILED")
        elif any(status == "formatted" for status in statuses):
            summary_lines.append("Overall status: FORMATTED")
        else:
            summary_lines.append("Overall status: SUCCESS")

        summary_lines.append("")
        summary_lines.append("")

        self.call_from_thread(self._update_widget, "#summary-content", "\n".join(summary_lines))

    def _update_widget(self, widget_id: str, content: str) -> None:
        """ウィジェットの内容を更新する安全なヘルパー。"""
        try:
            widget = self.query_one(widget_id, Log)
            widget.write(content, scroll_end=False)
            # 強制的に画面を更新
            self.refresh()
        except Exception as e:
            # ウィジェット更新エラーをログに記録
            logging.error(f"UIエラー: {widget_id}", exc_info=True)
            if self.fatal_error is None:
                self.fatal_error = f"Widget update error: {e}"

    def _update_tab_title(self, command: str, has_error: bool) -> None:
        """タブタイトルを更新（エラー時に*を追加）。"""
        # pylint: disable=protected-access
        try:
            tc = self.query_one(TabbedContent)
            tab = tc.get_tab(f"tab-{command}")
            if has_error:
                tab.label = f"{command} *"  # type: ignore[assignment]
            else:
                tab.label = command  # type: ignore[assignment]
        except Exception:
            logging.warning(f"タブタイトル更新失敗: {command}", exc_info=True)

    def _handle_fatal_error(self) -> None:
        """致命的エラー時の処理。"""
        logging.error("Handling fatal error - exiting application")
        # アプリケーションを終了
        self.exit(1)
