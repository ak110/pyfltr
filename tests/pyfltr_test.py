# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring

import pathlib
import subprocess

import pytest

import pyfltr.pyfltr


@pytest.mark.parametrize("mode", ["run", "ci", "pre-commit"])
def test_success(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=0, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.pyfltr.run(
        args=[mode, str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 0


@pytest.mark.parametrize("mode", ["run", "ci", "pre-commit"])
def test_fail(mocker, mode):
    proc = subprocess.CompletedProcess(["test"], returncode=-1, stdout="test")
    mocker.patch("subprocess.run", return_value=proc)
    returncode = pyfltr.pyfltr.run(
        args=[mode, str(pathlib.Path(__file__).parent.parent)]
    )
    assert returncode == 1
