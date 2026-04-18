# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=protected-access

import logging

import pyfltr.cli
import pyfltr.command
import pyfltr.config
from tests.conftest import make_command_result as _make_result


def test_write_log(caplog):
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
    with caplog.at_level(logging.DEBUG):
        pyfltr.cli.write_log(result)

    assert "pytest" in caplog.text
    assert "returncode: 0" in caplog.text


def test_write_log_failed(caplog):
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
    with caplog.at_level(logging.DEBUG):
        pyfltr.cli.write_log(result)

    # 失敗時は@マークが使われる
    assert "@ returncode: 1" in caplog.text


def test_run_one_command_stream_mode_writes_detail_log(mocker, caplog):
    """per_command_log=True のとき詳細ログを即時出力すること。"""
    result = _make_result("mypy", returncode=0, output="ok")
    mocker.patch("pyfltr.command.execute_command", return_value=result)
    mock_args = mocker.MagicMock()
    mock_config = mocker.MagicMock()

    with caplog.at_level(logging.INFO):
        pyfltr.cli._run_one_command("mypy", mock_args, mock_config, [], per_command_log=True)

    assert "mypy 実行中です..." in caplog.text
    # 成功時はエラーなし・生出力なしのため output は表示されない
    assert "* returncode: 0" in caplog.text


def test_run_one_command_buffer_mode_shows_only_progress(mocker, caplog):
    """per_command_log=False のとき開始/完了の 1 行進捗のみ出力すること。"""
    result = _make_result("mypy", returncode=0, output="ok")
    mocker.patch("pyfltr.command.execute_command", return_value=result)
    mock_args = mocker.MagicMock()
    mock_config = mocker.MagicMock()

    with caplog.at_level(logging.INFO):
        pyfltr.cli._run_one_command("mypy", mock_args, mock_config, [], per_command_log=False)

    assert "mypy 実行中です..." in caplog.text
    assert "mypy 完了" in caplog.text
    # 詳細ログ (output や returncode 行) は出ていない
    assert "ok" not in caplog.text
    assert "returncode: 0" not in caplog.text


def test_render_results_orders_success_failed_summary(caplog):
    """成功コマンド → 失敗コマンド → summary の順で出力されること。"""
    config = pyfltr.config.create_default_config()
    # 失敗コマンドはerrorsが空のため生出力がフォールバック表示される
    results = [
        _make_result("mypy", returncode=1, output="MYPY_ERROR"),
        _make_result("ruff-format", returncode=0, command_type="formatter"),
        _make_result("pylint", returncode=0),
    ]

    with caplog.at_level(logging.INFO):
        pyfltr.cli.render_results(results, config, include_details=True)

    text = caplog.text
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


def test_render_results_include_details_false_writes_only_summary(caplog):
    """include_details=False のときは summary のみで詳細ログは出さない。"""
    config = pyfltr.config.create_default_config()
    results = [_make_result("mypy", returncode=1, output="MYPY_ERROR")]

    with caplog.at_level(logging.INFO):
        pyfltr.cli.render_results(results, config, include_details=False)

    assert "summary" in caplog.text
    assert "MYPY_ERROR" not in caplog.text


def test_render_results_writes_warnings_section_before_summary(caplog):
    """warnings 引数が渡されると summary 直前に warnings セクションが出る。"""
    config = pyfltr.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]
    warnings = [{"source": "config", "message": "pre-commit 設定ファイル不在"}]

    with caplog.at_level(logging.INFO):
        pyfltr.cli.render_results(results, config, include_details=True, warnings=warnings)

    text = caplog.text
    warning_pos = text.index("pre-commit 設定ファイル不在")
    summary_pos = text.index("summary")
    assert warning_pos < summary_pos
    assert "[config]" in text


def test_render_results_skips_warnings_section_when_empty(caplog):
    """warnings が空のときは warnings 見出しを出さない。"""
    config = pyfltr.config.create_default_config()
    results = [_make_result("mypy", returncode=0)]

    with caplog.at_level(logging.INFO):
        pyfltr.cli.render_results(results, config, include_details=True, warnings=[])

    # warnings セクションは出力されない（summary 直前の見出しだけを検証するのは困難なため、
    # [source] 形式のエントリ行が無いことで代替する）
    assert "[config]" not in caplog.text


def test_write_summary_shows_run_id_guidance_when_present(caplog):
    """`run_id` が存在するとき run_id と誘導文言をログに出力すること。"""
    results = [_make_result("mypy", returncode=0)]
    with caplog.at_level(logging.INFO):
        pyfltr.cli._write_summary(results, run_id="01JABCDEFGH")

    assert "run_id: 01JABCDEFGH" in caplog.text
    assert "pyfltr show-run latest" in caplog.text


def test_write_summary_omits_run_id_guidance_when_none(caplog):
    """`run_id` が None のとき run_id 関連行をログに出力しないこと。"""
    results = [_make_result("mypy", returncode=0)]
    with caplog.at_level(logging.INFO):
        pyfltr.cli._write_summary(results, run_id=None)

    assert "run_id" not in caplog.text
    assert "show-run" not in caplog.text


def test_run_commands_with_cli_fail_fast_aborts_remaining_fixers(mocker):
    """--fail-fast 発動時、fix ステージのエラーで後続の formatter/linter を skipped 化する。"""
    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True
    config.values["ruff-format"] = True
    config.values["mypy"] = True
    # fix ステージで ruff-check が has_error=True で返る想定
    fix_fail = _make_result("ruff-check", returncode=1, command_type="linter")
    mocker.patch("pyfltr.command.execute_command", return_value=fix_fail)
    mock_args = mocker.MagicMock()

    results = pyfltr.cli.run_commands_with_cli(
        ["ruff-check", "ruff-format", "mypy"],
        mock_args,
        config,
        [],
        per_command_log=False,
        include_fix_stage=True,
        fail_fast=True,
    )
    # 通常ステージはスキップされ、ruff-format と mypy が skipped で積まれる
    statuses = {r.command: r.status for r in results}
    assert statuses.get("ruff-format") == "skipped"
    assert statuses.get("mypy") == "skipped"


def test_run_commands_with_cli_without_fail_fast_continues(mocker):
    """fail_fast=False なら1ツール失敗でも後続が走る。"""
    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    config.values["mypy"] = True
    fail_result = _make_result("ruff-format", returncode=1, command_type="formatter", has_error=True)
    success = _make_result("mypy", returncode=0)

    def _fake_execute(command, *_args, **_kwargs):
        return fail_result if command == "ruff-format" else success

    mocker.patch("pyfltr.command.execute_command", side_effect=_fake_execute)
    mock_args = mocker.MagicMock()

    results = pyfltr.cli.run_commands_with_cli(
        ["ruff-format", "mypy"],
        mock_args,
        config,
        [],
        per_command_log=False,
        include_fix_stage=False,
        fail_fast=False,
    )
    commands = [r.command for r in results]
    assert "mypy" in commands
    assert "ruff-format" in commands
    assert not any(r.status == "skipped" for r in results)
