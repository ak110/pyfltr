"""CLIテストの共通fixture。"""

import pathlib

import pytest


@pytest.fixture(name="mcp_cache")
def _mcp_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    """`PYFLTR_CACHE_DIR`を`tmp_path`に固定するフィクスチャ。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path
