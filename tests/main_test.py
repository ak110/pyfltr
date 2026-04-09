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


def test_fix_mode_with_no_eligible_commands(mocker, caplog):
    """fix モードで対象コマンドが 0 件なら exit 1 にする。"""
    # formatter を全部 disabled にすれば fix 対象なしになる
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.main.run(["--fix", "--commands=mypy,pylint", str(pathlib.Path(__file__).parent.parent)])
    # mypy/pylint は fix-args 未定義なので 0 件
    assert returncode == 1
    assert "--fix で実行可能なコマンドがありません" in caplog.text


def test_fix_mode_disables_shuffle(mocker, caplog):
    """--fix と --shuffle を同時指定した場合、shuffle が無効化される。"""
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="")
    mocker.patch("subprocess.run", return_value=proc)
    # ruff-format は pyfltr 自身の preset=latest で有効化されている
    returncode = pyfltr.main.run(["--fix", "--shuffle", "--commands=ruff-format", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    assert "--shuffle を無効化" in caplog.text


def test_stream_mode_writes_detail_log_during_run(mocker, caplog):
    """--stream 指定時はコマンド完了時に詳細ログが出力される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(["--no-ui", "--stream", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    # 詳細ログに含まれる returncode 行が出力される
    assert "returncode: 0" in caplog.text
    # summary セクションも引き続き出力される
    assert "summary" in caplog.text


def test_buffered_mode_is_default(mocker, caplog):
    """既定では summary → 成功コマンド詳細の順でまとめて出力される。"""
    proc = subprocess.CompletedProcess(["mypy"], returncode=0, stdout="mypy-detail")
    mocker.patch("subprocess.run", return_value=proc)

    returncode = pyfltr.main.run(["--no-ui", "--commands=mypy", str(pathlib.Path(__file__).parent.parent)])
    assert returncode == 0
    text = caplog.text
    # summary セクションが詳細ログより先に来る
    assert "summary" in text
    assert "returncode: 0" in text
    assert text.index("summary") < text.index("returncode: 0")


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
