# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring

import logging

import pyfltr.cli
import pyfltr.command


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


def test_run_command_for_cli_logs_start(mocker, caplog):
    """run_command_for_cliが開始/終了メッセージを出力すること。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy", "test.py"],
        returncode=0,
        has_error=False,
        files=1,
        output="ok",
        elapsed=0.5,
    )
    mocker.patch("pyfltr.command.execute_command", return_value=result)
    mock_args = mocker.MagicMock()
    mock_config = mocker.MagicMock()

    with caplog.at_level(logging.INFO):
        pyfltr.cli.run_command_for_cli("mypy", mock_args, mock_config)

    assert "Running mypy..." in caplog.text
    assert "ok" in caplog.text
    assert "* returncode: 0" in caplog.text
