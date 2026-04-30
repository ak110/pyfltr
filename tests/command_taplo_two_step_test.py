"""command.py の taplo 2段階実行テスト。

`execute_taplo_two_step` の動作を検証する。
"""

import os
import pathlib
import subprocess

import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.config.config
from tests import conftest as _testconf


def test_taplo_two_step_check_clean(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 (taplo check) rc=0 → succeeded。Step2 (format) は実行されない。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname = "foo"\n')

    proc = subprocess.CompletedProcess(["taplo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "format" not in cmdline
    assert result.status == "succeeded"
    assert result.has_error is False


def test_taplo_two_step_check_needs_format(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 → Step2 (format) を実行。rc=0 かつ内容変化ありなら formatted。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname="foo"\n')

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="Cargo.toml is not formatted")
        # format ステップ: ファイルを書き換えてシミュレート
        target.write_text('[package]\nname = "foo"\n')
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    # スナップショット取得後に内容が変化するよう mtime を事前に固定する
    os.utime(target, (1000000000, 1000000000))

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "formatted"
    assert result.has_error is False
    assert result.fixed_files is not None and str(target) in result.fixed_files


def test_taplo_two_step_step2_failure_marks_failed(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 でも Step2 の rc!=0 なら failed。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname="foo"\n')

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout="format error")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.has_error is True


def test_taplo_fix_mode_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードでは Step1 (check) をスキップし直接 format を実行する。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname="foo"\n')
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        # format 実行時にファイルを書き換えてシミュレート
        target.write_text('[package]\nname = "foo"\n')
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    # 1 回だけ呼ばれる（Step1 スキップ）
    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "format" in cmdline
    assert "check" not in cmdline
    # ハッシュ変化ありなので formatted
    assert result.status == "formatted"
    assert result.fixed_files is not None and str(target) in result.fixed_files


def test_taplo_fix_mode_no_change_succeeds(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードで format が走ってもハッシュ変化が無ければ succeeded。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname = "foo"\n')

    proc = subprocess.CompletedProcess(["taplo"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "succeeded"
    assert result.fixed_files == []


def test_taplo_fix_mode_process_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードで format がエラー終了した場合は failed。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname = "foo"\n')

    proc = subprocess.CompletedProcess(["taplo"], returncode=1, stdout="syntax error")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "failed"
    assert result.has_error is True
