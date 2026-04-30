"""command.py の ruff-format 2段階実行テスト。

`_execute_ruff_format_two_step` の動作を検証する。
"""

# pylint: disable=protected-access,duplicate-code

import os
import pathlib
import subprocess

import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.config.config
from tests import conftest as _testconf


def test_ruff_format_two_step_runs_check_and_format(mocker, tmp_path: pathlib.Path) -> None:
    """ruff-format-by-check=true のとき ruff check と ruff format の両方が実行される。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process._run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    # subprocess は 2 回呼ばれる (check ステップ + format ステップ)
    assert mock_run.call_count == 2
    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]
    assert "check" in step1_cmdline
    assert "--fix" in step1_cmdline
    assert "--unsafe-fixes" in step1_cmdline
    assert "format" in step2_cmdline
    assert "--exit-non-zero-on-format" in step2_cmdline
    # status はどちらも exit 0 なので succeeded
    assert result.status == "succeeded"


def test_ruff_format_by_check_false_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    """ruff-format-by-check=false のとき ruff format のみが実行される。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process._run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    config.values["ruff-format-by-check"] = False
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    # subprocess は 1 回のみ (format ステップのみ)
    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "format" in cmdline
    assert "check" not in cmdline
    assert result.status == "succeeded"


def test_ruff_format_step1_lint_violation_ignored(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ1の lint violation (exit 1) は失敗扱いしない。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="lint violation")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    # ステップ1の exit 1 は無視され、ステップ2の exit 0 が反映されて succeeded
    assert result.status == "succeeded"
    assert result.has_error is False


def test_ruff_format_step1_internal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ1の exit 2 (設定ミス等) は failed 扱い。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=2, stdout="usage error")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.has_error is True


def test_ruff_format_step2_internal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ2の exit 2 も failed 扱い。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="format error")

    mocker.patch("pyfltr.command.process._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.has_error is True


def test_ruff_format_step1_mtime_change_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ1でファイルが書き換わった場合、formatted 扱いになる。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")
    # ファイルシステムの mtime 分解能の影響で同一ナノ秒に収まるケースを避けるため、
    # 事前に古めの mtime を設定しておく (テストの決定性担保)。
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "check" in cmdline:
            # ruff check が修正を適用したことをシミュレート: 明示的に新しい mtime を設定。
            target.write_text("x = 2\n")
            os.utime(target, (2000000000, 2000000000))
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-format", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    # mtime が変化したので formatted
    assert result.status == "formatted"
    assert result.has_error is False
