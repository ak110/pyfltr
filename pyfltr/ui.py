"""Textual UI関連の処理。"""
# cli.py と ui.py は並列実行・fail-fast・skipped 生成の責務が対称的に重複するが、
# パート D のスコープではそのまま残す方針 (統合は別パートで扱う)。
# pylint: disable=duplicate-code

import argparse
import concurrent.futures
import logging
import pathlib
import sys
import threading
import time
import traceback
import typing

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Log, TabbedContent, TabPane

import pyfltr.cache
import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.executor
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
    config: pyfltr.config.Config,
    all_files: list[pathlib.Path],
    *,
    archive_hook: typing.Callable[[pyfltr.command.CommandResult], None] | None = None,
    cache_store: pyfltr.cache.CacheStore | None = None,
    cache_run_id: str | None = None,
    fail_fast: bool = False,
    only_failed_targets: dict[str, list[pathlib.Path] | None] | None = None,
) -> tuple[list[pyfltr.command.CommandResult], int]:
    """UI付きでコマンドを実行。

    ``archive_hook`` が指定されている場合、各コマンド完了時に実行アーカイブへ書き出す
    (fix ステージも含めて全実行を保存する)。キャッシュヒット時の結果はアーカイブには
    書き込まない (``cached_from`` でソース run を参照させる前提)。

    ``fail_fast=True`` のとき、いずれかのツールが ``has_error=True`` で完了した時点で
    未実行ジョブを ``future.cancel()`` で打ち切り、起動済みサブプロセスに
    ``terminate()`` を送る。

    ``only_failed_targets`` が指定された場合、ツール別の失敗ファイル集合を
    ``execute_command`` へ流す (``--only-failed`` 経路)。値が ``None`` のツールは通常の
    ``all_files`` で実行し、``list`` のツールはその集合のみを対象にする。
    """
    app = UIApp(
        commands,
        args,
        config,
        all_files,
        archive_hook=archive_hook,
        cache_store=cache_store,
        cache_run_id=cache_run_id,
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
    except Exception:
        # Textualアプリケーション自体の例外処理
        error_msg = f"UI アプリケーションの実行に失敗しました: {traceback.format_exc()}"
        logging.error(error_msg)
        print(f"エラー: {error_msg}", file=sys.stderr)
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

    def __init__(
        self,
        commands: list[str],
        args: argparse.Namespace,
        config: pyfltr.config.Config,
        all_files: list[pathlib.Path],
        *,
        archive_hook: typing.Callable[[pyfltr.command.CommandResult], None] | None = None,
        cache_store: pyfltr.cache.CacheStore | None = None,
        cache_run_id: str | None = None,
        fail_fast: bool = False,
        only_failed_targets: dict[str, list[pathlib.Path] | None] | None = None,
    ) -> None:
        super().__init__()
        self.commands = commands
        self.args = args
        self.config = config
        self._all_files = all_files
        self._archive_hook = archive_hook
        self._cache_store = cache_store
        self._cache_run_id = cache_run_id
        self._fail_fast = fail_fast
        self._only_failed_targets = only_failed_targets
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
                self.notify("終了するには 1 秒以内にもう一度 Ctrl+C を押してください。")

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
            fixers, formatters, linters_and_testers = pyfltr.executor.split_commands_for_execution(
                self.commands, self.config, self._all_files, include_fix_stage=include_fix_stage
            )
            aborted = False

            # fix ステージ (serial)。結果は summary に含めず、後段の通常ステージに委ねる。
            # アーカイブには fix ステージも含めて全実行を保存する。
            for command in fixers:
                fix_result = self._execute_command(command, fix_stage=True)
                if self._archive_hook is not None and not fix_result.cached:
                    self._archive_hook(fix_result)
                if self._fail_fast and fix_result.has_error:
                    aborted = True
                    self._skip_remaining([*formatters, *linters_and_testers])
                    break

            # formatters (serial)
            if not aborted:
                for idx, command in enumerate(formatters):
                    fmt_result = self._execute_command(command)
                    self.results.append(fmt_result)
                    if self._archive_hook is not None and not fmt_result.cached:
                        self._archive_hook(fmt_result)
                    if self._fail_fast and fmt_result.has_error:
                        aborted = True
                        self._skip_remaining([*formatters[idx + 1 :], *linters_and_testers])
                        break

            # linters/testers (parallel)
            if not aborted and len(linters_and_testers) > 0:
                aborted_commands: set[str] = set()
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.config["jobs"]) as executor:
                    future_to_command = {
                        executor.submit(self._execute_command, command): command for command in linters_and_testers
                    }
                    for future in concurrent.futures.as_completed(future_to_command):
                        try:
                            lt_result = future.result()
                        except concurrent.futures.CancelledError:
                            aborted_commands.add(future_to_command[future])
                            continue
                        self.results.append(lt_result)
                        if self._archive_hook is not None and not lt_result.cached:
                            self._archive_hook(lt_result)
                        if self._fail_fast and not aborted and lt_result.has_error:
                            aborted = True
                            for pending_future, pending_command in future_to_command.items():
                                if pending_future.done():
                                    continue
                                if pending_future.cancel():
                                    aborted_commands.add(pending_command)
                            pyfltr.command.terminate_active_processes()
                if aborted_commands:
                    for pending_command in aborted_commands:
                        skipped = self._make_skipped_result(pending_command)
                        self.results.append(skipped)
                        if self._archive_hook is not None:
                            self._archive_hook(skipped)
                        self.call_from_thread(
                            self._update_summary,
                            pending_command,
                            "skipped",
                            0,
                            0.0,
                        )

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
            error_msg = f"UI 処理中に致命的エラーが発生しました:\n{traceback.format_exc()}"
            try:
                self.call_from_thread(self._handle_fatal_error, error_msg)
            except Exception:
                logging.error(error_msg)

    def _execute_command(self, command: str, *, fix_stage: bool = False) -> pyfltr.command.CommandResult:
        """出力をキャプチャしながらコマンド実行。"""
        # serial_group を持つコマンドは同一グループ内で排他実行される (cargo / dotnet 等)。
        # ロック取得前は「待機中」の表示に留め、running 表示はロック取得後に切り替える。
        with pyfltr.executor.serial_group_lock(self.config.commands[command].serial_group):
            # Summaryを「running」に更新
            self._start_times[command] = time.perf_counter()
            self.call_from_thread(self._update_summary, command, "running")

            # コマンドタブに開始メッセージを出力
            self.call_from_thread(
                self._write_log,
                f"#output-{command}",
                f"{command} を実行中です...\n",
            )

            # JSON パーサー対応ツールではストリーミング出力を抑制し、
            # 完了後に ErrorLocation ベースの表示に切り替える。
            has_custom_parser = command in pyfltr.error_parser.get_custom_parser_commands()
            callback: typing.Callable[[str], None] | None = None
            if not has_custom_parser:

                def _on_output(line: str) -> None:
                    """出力行をリアルタイムでUIに反映。"""
                    self.call_from_thread(self._write_log, f"#output-{command}", line.removesuffix("\n"))

                callback = _on_output

            result = pyfltr.command.execute_command(
                command,
                self.args,
                self.config,
                self._all_files,
                on_output=callback,
                fix_stage=fix_stage,
                cache_store=self._cache_store,
                cache_run_id=self._cache_run_id,
                only_failed_files=pyfltr.command.pick_only_failed_files(self._only_failed_targets, command),
            )
        # ここ以降は結果の UI 反映のみなので serial_group ロックの外で行う。

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

            # JSON パーサー対応ツールはストリーミングしていないため、
            # ErrorLocation ベースの表示または生出力フォールバックを書き出す。
            if has_custom_parser:
                self.call_from_thread(self._clear_log, f"#output-{command}")
                if result.errors:
                    lines = [pyfltr.error_parser.format_error(e) for e in result.errors]
                    self.call_from_thread(self._write_log, f"#output-{command}", "\n".join(lines))
                elif result.alerted:
                    self.call_from_thread(self._write_log, f"#output-{command}", result.output)
                else:
                    summary = pyfltr.error_parser.parse_summary(command, result.output)
                    if summary:
                        self.call_from_thread(self._write_log, f"#output-{command}", summary)

            # フッター情報を追記
            footer = f"{'-' * 40}\n終了コード: {result.returncode}\nステータス: {result.get_status_text()}\n"
            self.call_from_thread(
                self._write_log,
                f"#output-{result.command}",
                footer,
            )
            # コマンド失敗時のタブタイトル更新
            if result.status == "failed":
                self.call_from_thread(self._update_tab_title, result.command)

            # エラーまたは警告があればErrorsタブを即時追加/更新
            if result.errors:
                self._all_errors.extend(result.errors)
            sorted_errors = pyfltr.error_parser.sort_errors(self._all_errors, self.config.command_names)
            current_warnings = pyfltr.warnings_.collected_warnings()
            if sorted_errors or current_warnings:
                self.call_from_thread(self._update_errors_tab, sorted_errors, current_warnings)  # type: ignore[arg-type]

        return result

    async def _update_errors_tab(
        self,
        errors: list[pyfltr.error_parser.ErrorLocation],
        warnings: list[dict[str, typing.Any]],
    ) -> None:
        """Errors タブを追加または更新。初回のみアクティブに切り替え。

        警告は errors の後ろに「warnings:」セクションとして追記する。
        """
        tc = self.query_one(TabbedContent)
        sections: list[str] = [pyfltr.error_parser.format_error(e) for e in errors]
        if warnings:
            if sections:
                sections.append("")
            sections.append("warnings:")
            sections.extend(f"    [{entry['source']}] {entry['message']}" for entry in warnings)
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
            tab.label = f"{command} *"  # type: ignore[assignment]
        except Exception:
            logging.warning(f"タブタイトル更新失敗: {command}", exc_info=True)

    def _handle_fatal_error(self, msg: str) -> None:
        """致命的エラー時の処理。"""
        logging.error(f"致命的エラーが発生しました: {msg}")
        # アプリケーションを終了
        self.exit(return_code=1)

    def _skip_remaining(self, commands: list[str]) -> None:
        """--fail-fast 中断時、未実行ツールを skipped として登録する (fix/formatter 段から)。"""
        pyfltr.command.terminate_active_processes()
        for command in commands:
            skipped = self._make_skipped_result(command)
            self.results.append(skipped)
            if self._archive_hook is not None:
                self._archive_hook(skipped)
            self.call_from_thread(self._update_summary, command, "skipped", 0, 0.0)

    def _make_skipped_result(self, command: str) -> pyfltr.command.CommandResult:
        """--fail-fast 中断対象の skipped CommandResult を作る。"""
        command_info = self.config.commands[command]
        return pyfltr.command.CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=[],
            returncode=None,
            has_error=False,
            files=0,
            output="--fail-fast により実行をスキップしました。",
            elapsed=0.0,
        )
