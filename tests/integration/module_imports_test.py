"""新規プロセスで公開された内部入口の循環importを検出する。"""

import pathlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "pyfltr.parsing.entry",
        "pyfltr.parsing.tools",
        "pyfltr.parsing.pytest",
        "pyfltr.rule_urls",
        "pyfltr.tools",
        "pyfltr.config.model",
    ],
)
def test_modules_import_in_fresh_process(module: str) -> None:
    """他の入口を先行importしなくても各入口を読み込める。"""
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=pathlib.Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
