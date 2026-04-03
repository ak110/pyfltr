# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring

import pathlib
import subprocess

import pytest

import pyfltr.main


@pytest.mark.parametrize("mode", ["run", "ci", "pre-commit"])
def test_success(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0


@pytest.mark.parametrize("mode", ["run", "ci", "pre-commit"])
def test_fail(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=-1, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.main.run([mode, str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 1


def test_work_dir(mocker, tmp_path):
    """--work-dirオプションのテスト。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n')
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    original_cwd = pathlib.Path.cwd()
    returncode = pyfltr.main.run(["--work-dir", str(tmp_path), "--commands=pytest", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    # cwdが復元されていることを確認
    assert pathlib.Path.cwd() == original_cwd


def test_additional_args(mocker):
    """追加引数のテスト。"""
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="test")
    mock_run = mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(
        ["--commands=pytest", "--pytest-args=--maxfail=5 -v", str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0

    # subprocess.runが呼ばれた引数を確認
    assert mock_run.called
    called_args = mock_run.call_args[0][0]  # 最初の引数（コマンドライン）
    assert "--maxfail=5" in called_args
    assert "-v" in called_args
