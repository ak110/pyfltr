# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=protected-access

import collections.abc
import logging

import pytest

import pyfltr.cli.output_format
import pyfltr.cli.pipeline
import pyfltr.command
import pyfltr.config.config
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_execution_context as _make_ctx


@pytest.fixture(name="text_logs")
def _text_logs() -> collections.abc.Iterator[list[str]]:
    """`pyfltr.cli.output_format.text_logger`の`info`出力をキャプチャする。

    `propagate=False`のためcaplog / capsysのsys.stdout差し替えとは相性が悪い
    （pytest captureはsetup段階のsys.stdoutリファレンスと実行時のsys.stdoutが
    一致しない）。本fixtureはtext_loggerに専用ListHandlerを直接追加し、
    各テスト終了時に取り外すことで副作用を残さない。

    Returns:
        現在のテスト内でtext_loggerが記録したメッセージ文字列のリスト。
        （`logging.Handler.format`を通すことで`%`フォーマット差分を吸収する）。
    """
    messages: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(self.format(record))

    handler = _ListHandler(level=logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(message)s"))
    pyfltr.cli.output_format.text_logger.addHandler(handler)
    original_level = pyfltr.cli.output_format.text_logger.level
    pyfltr.cli.output_format.text_logger.setLevel(logging.DEBUG)
    try:
        yield messages
    finally:
        pyfltr.cli.output_format.text_logger.removeHandler(handler)
        pyfltr.cli.output_format.text_logger.setLevel(original_level)


def test_write_log(text_logs):
    """write_logの出力確認。"""
    result = pyfltr.command.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=["pytest", "test.py"],
        returncode=0,
        has_error=False,
        files=3,
        output="ok",
        elapsed=1.5,
    )
    pyfltr.cli.pipeline.write_log(result)
    text = "\n".join(text_logs)
    assert "pytest" in text
    assert "returncode: 0" in text


def test_write_log_failed(text_logs):
    """write_logの失敗時の出力確認。"""
    result = pyfltr.command.CommandResult(
        command="pytest",
        command_type="tester",
        commandline=["pytest", "test.py"],
        returncode=1,
        has_error=True,
        files=2,
        output="FAILED",
        elapsed=0.8,
    )
    pyfltr.cli.pipeline.write_log(result)
    # 失敗時は@マークが使われる
    assert "@ returncode: 1" in "\n".join(text_logs)


def test_run_one_command_stream_mode_writes_detail_log(mocker, text_logs):
    """per_command_log=Trueのとき詳細ログを即時出力すること。"""
    result = _make_result("mypy", returncode=0, output="ok")
    mocker.patch("pyfltr.command.execute_command", return_value=result)
    mock_args = mocker.MagicMock()
    mock_args.output_format = "text"
    mock_config = mocker.MagicMock()

    base_ctx = pyfltr.command.ExecutionBaseContext(config=mock_config, all_files=[], cache_store=None, cache_run_id=None)
    pyfltr.cli.pipeline._run_one_command("mypy", mock_args, base_ctx, per_command_log=True)
    text = "\n".join(text_logs)
    assert "mypy 実行中です..." in text
    # 成功時はエラーなし・生出力なしのためoutputは表示されない
    assert "* returncode: 0" in text


def test_run_one_command_buffer_mode_shows_only_progress(mocker, text_logs):
    """per_command_log=Falseのとき開始/完了の1行進捗のみ出力すること。"""
    result = _make_result("mypy", returncode=0, output="ok")
    mocker.patch("pyfltr.command.execute_command", return_value=result)
    mock_args = mocker.MagicMock()
    mock_args.output_format = "text"
    mock_config = mocker.MagicMock()

    base_ctx = pyfltr.command.ExecutionBaseContext(config=mock_config, all_files=[], cache_store=None, cache_run_id=None)
    pyfltr.cli.pipeline._run_one_command("mypy", mock_args, base_ctx, per_command_log=False)
    text = "\n".join(text_logs)
    assert "mypy 実行中です..." in text
    assert "mypy 完了" in text
    # 詳細ログ（outputやreturncode行）は出ていない
    assert "ok" not in text
    assert "returncode: 0" not in text


def test_render_results_orders_success_failed_summary(text_logs):
    """成功コマンド → 失敗コマンド → summary の順で出力されること。"""
    config = pyfltr.config.config.create_default_config()
    # 失敗コマンドはerrorsが空のため、生出力がフォールバック表示される
    results = [
        _make_result("mypy", returncode=1, output="MYPY_ERROR"),
        _make_result("ruff-format", returncode=0, command_type="formatter"),
        _make_result("pylint", returncode=0),
    ]

    pyfltr.cli.pipeline.render_results(results, config, include_details=True)

    text = "\n".join(text_logs)
    # 成功コマンドのヘッダーが最初に来る
    ruff_format_pos = text.index("ruff-format")
    pylint_pos = text.index("pylint")
    # 失敗コマンドの生出力がフォールバック表示される
    mypy_pos = text.index("MYPY_ERROR")
    # summary が末尾に来る
    summary_pos = text.index("summary")

    assert ruff_format_pos < mypy_pos
    assert pylint_pos < mypy_pos
    assert mypy_pos < summary_pos


def test_render_results_include_details_false_writes_only_summary(text_logs):
    """include_details=Falseのときはsummaryのみで詳細ログは出さない。"""
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=1, output="MYPY_ERROR")]

    pyfltr.cli.pipeline.render_results(results, config, include_details=False)
    text = "\n".join(text_logs)
    assert "summary" in text
    assert "MYPY_ERROR" not in text


def test_render_results_writes_warnings_section_before_summary(text_logs):
    """warnings引数が渡されるとsummary直前にwarningsセクションが出る。"""
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]
    warnings = [{"source": "config", "message": "pre-commit 設定ファイル不在"}]

    pyfltr.cli.pipeline.render_results(results, config, include_details=True, warnings=warnings)

    text = "\n".join(text_logs)
    warning_pos = text.index("pre-commit 設定ファイル不在")
    summary_pos = text.index("summary")
    assert warning_pos < summary_pos
    assert "[config]" in text


def test_render_results_skips_warnings_section_when_empty(text_logs):
    """warningsが空のときはwarnings見出しを出さない。"""
    config = pyfltr.config.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]

    pyfltr.cli.pipeline.render_results(results, config, include_details=True, warnings=[])

    # warningsセクションは出力されない（summary直前の見出しだけを検証するのは困難なため、
    # [source]形式のエントリ行が無いことで代替する）
    assert "[config]" not in "\n".join(text_logs)


def test_run_commands_with_cli_fail_fast_aborts_remaining_fixers(mocker):
    """--fail-fast発動時、fixステージのエラーで後続のformatter/linterをskipped化する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-format"] = True
    config.values["mypy"] = True
    # fixステージでruff-checkがhas_error=Trueで返る想定
    fix_fail = _make_result("ruff-check", returncode=1, command_type="linter")
    mocker.patch("pyfltr.command.execute_command", return_value=fix_fail)
    mock_args = mocker.MagicMock()

    base_ctx = _make_ctx(config, []).base
    results = pyfltr.cli.pipeline.run_commands_with_cli(
        ["ruff-check", "ruff-format", "mypy"],
        mock_args,
        base_ctx,
        per_command_log=False,
        include_fix_stage=True,
        fail_fast=True,
    )
    # 通常ステージはスキップされ、ruff-formatとmypyがskippedで積まれる
    statuses = {r.command: r.status for r in results}
    assert statuses.get("ruff-format") == "skipped"
    assert statuses.get("mypy") == "skipped"


def test_run_commands_with_cli_without_fail_fast_continues(mocker):
    """fail_fast=Falseなら1ツール失敗でも後続が走る。"""
    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    config.values["mypy"] = True
    fail_result = _make_result("ruff-format", returncode=1, command_type="formatter", has_error=True)
    success = _make_result("mypy", returncode=0)

    def _fake_execute(command, *_args, **_kwargs):
        return fail_result if command == "ruff-format" else success

    mocker.patch("pyfltr.command.execute_command", side_effect=_fake_execute)
    mock_args = mocker.MagicMock()

    base_ctx = _make_ctx(config, []).base
    results = pyfltr.cli.pipeline.run_commands_with_cli(
        ["ruff-format", "mypy"],
        mock_args,
        base_ctx,
        per_command_log=False,
        include_fix_stage=False,
        fail_fast=False,
    )
    commands = [r.command for r in results]
    assert "mypy" in commands
    assert "ruff-format" in commands
    assert not any(r.status == "skipped" for r in results)
