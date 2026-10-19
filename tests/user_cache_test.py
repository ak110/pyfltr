import pathlib

import pytest

import pyfltr.user_cache


def test_default_cache_root_respects_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """PYFLTR_CACHE_DIR環境変数がdefault_cache_rootで優先される。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    result = pyfltr.user_cache.default_cache_root()
    assert result == tmp_path
