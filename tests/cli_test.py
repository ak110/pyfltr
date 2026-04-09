# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring
# pylint: disable=protected-access

import logging

import pyfltr.cli
import pyfltr.command
import pyfltr.config


def _make_result(
    command: str,
    *,
    returncode: int,
    output: str = "",
    command_type: str = "linter",
) -> pyfltr.command.CommandResult:
    """テスト用の CommandResult を生成。"""
    return pyfltr.command.CommandResult(
        command=command,
        command_type=command_type,
        commandline=[command],
        returncode=returncode,
        has_error=returncode != 0,
        files=1,
        output=output,
        elapsed=0.1,
    )


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
        pyfltr.cli._run_one_command("mypy", mock_args, mock_config, per_command_log=True)

    assert "mypy 実行中です..." in caplog.text
    assert "ok" in caplog.text
    assert "* returncode: 0" in caplog.text


def test_run_one_command_buffer_mode_shows_only_progress(mocker, caplog):
    """per_command_log=False のとき開始/完了の 1 行進捗のみ出力すること。"""
    result = _make_result("mypy", returncode=0, output="ok")
    mocker.patch("pyfltr.command.execute_command", return_value=result)
    mock_args = mocker.MagicMock()
    mock_config = mocker.MagicMock()

    with caplog.at_level(logging.INFO):
        pyfltr.cli._run_one_command("mypy", mock_args, mock_config, per_command_log=False)

    assert "mypy 実行中です..." in caplog.text
    assert "mypy 完了" in caplog.text
    # 詳細ログ (output や returncode 行) は出ていない
    assert "ok" not in caplog.text
    assert "returncode: 0" not in caplog.text


def test_render_results_orders_summary_success_failed(caplog):
    """summary → 成功コマンド → 失敗コマンドの順で出力されること。"""
    config = pyfltr.config.create_default_config()
    results = [
        _make_result("mypy", returncode=1, output="MYPY_ERROR"),
        _make_result("black", returncode=0, output="BLACK_OK", command_type="formatter"),
        _make_result("pylint", returncode=0, output="PYLINT_OK"),
    ]

    with caplog.at_level(logging.INFO):
        pyfltr.cli.render_results(results, config, include_details=True)

    text = caplog.text
    # summary が最初に来る
    summary_pos = text.index("summary")
    # 成功コマンドの出力が summary の後
    black_pos = text.index("BLACK_OK")
    pylint_pos = text.index("PYLINT_OK")
    # 失敗コマンドの出力が最後に来る
    mypy_pos = text.index("MYPY_ERROR")

    assert summary_pos < black_pos
    assert summary_pos < pylint_pos
    assert black_pos < mypy_pos
    assert pylint_pos < mypy_pos


def test_render_results_include_details_false_writes_only_summary(caplog):
    """include_details=False のときは summary のみで詳細ログは出さない。"""
    config = pyfltr.config.create_default_config()
    results = [_make_result("mypy", returncode=1, output="MYPY_ERROR")]

    with caplog.at_level(logging.INFO):
        pyfltr.cli.render_results(results, config, include_details=False)

    assert "summary" in caplog.text
    assert "MYPY_ERROR" not in caplog.text
