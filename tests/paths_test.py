"""paths モジュールのテストコード。"""

import pathlib

import pytest

import pyfltr.paths


@pytest.mark.parametrize(
    "path,expected",
    [
        # Windowsパス: バックスラッシュをスラッシュへ変換する
        ("a\\b\\c", "a/b/c"),
        # Unixパス: 変換不要のためそのまま返す
        ("a/b/c", "a/b/c"),
        # 混在パス: バックスラッシュのみをスラッシュへ変換する
        ("a\\b/c", "a/b/c"),
        # 空文字: そのまま返す
        ("", ""),
        # ファイル名のみ（区切り文字なし）: そのまま返す
        ("foo.py", "foo.py"),
    ],
)
def test_normalize_separators_str(path: str, expected: str) -> None:
    """str型引数の変換パターンを検証する。"""
    assert pyfltr.paths.normalize_separators(path) == expected


def test_normalize_separators_pathlib() -> None:
    """pathlib.Path型引数を受け付けることを検証する。"""
    result = pyfltr.paths.normalize_separators(pathlib.Path("a/b/c"))
    assert result == "a/b/c"
