"""command.py の prettier 2段階実行テスト。

`execute_prettier_two_step` の動作を検証する。
"""

import os
import pathlib
import subprocess

import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.config.config
from tests import conftest as _testconf


def test_prettier_two_step_check_clean(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 (prettier --check) rc=0 → succeeded。Step2 (--write) は実行されない。"""
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    proc = subprocess.CompletedProcess(["prettier"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--check" in cmdline
    assert "--write" not in cmdline
    assert result.status == "succeeded"
    assert result.has_error is False


def test_prettier_two_step_check_needs_write(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 → Step2 (--write) を実行。rc=0 なら formatted。"""
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        if "--check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="[warn] sample.js")
        # --write step
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="sample.js")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "formatted"
    assert result.has_error is False


def test_prettier_two_step_check_rc2_fails_without_write(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc>=2 (致命的エラー) → failed、Step2 は実行しない。"""
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    calls: list[list[str]] = []

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        calls.append(cmdline)
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="SyntaxError")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.has_error is True
    # Step2 は実行されない
    assert len(calls) == 1
    assert "--check" in calls[0]


def test_prettier_two_step_step2_failure_marks_failed(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 でも Step2 の rc>=2 なら failed。"""
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        if "--check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="write failed")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.has_error is True


def test_prettier_fix_mode_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードでは Step1 (--check) をスキップし直接 --write を実行する。"""
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        # --write 実行時にファイルを書き換えたことをシミュレート
        target.write_text("x = 1;\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    # 1 回だけ呼ばれる (Step1 スキップ)
    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--write" in cmdline
    assert "--check" not in cmdline
    # ハッシュ変化ありなので formatted
    assert result.status == "formatted"


def test_prettier_fix_mode_no_change_succeeds(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードで --write が走ってもハッシュ変化が無ければ succeeded。"""
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    proc = subprocess.CompletedProcess(["prettier"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "prettier", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "succeeded"
