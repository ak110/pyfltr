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


def test_to_cwd_relative_converts_absolute_under_cwd() -> None:
    """cwd配下の絶対パスは相対パスへ変換される。"""
    absolute = pathlib.Path.cwd() / "pyfltr" / "paths.py"
    assert pyfltr.paths.to_cwd_relative(absolute) == "pyfltr/paths.py"


def test_to_cwd_relative_keeps_relative_as_is() -> None:
    """相対パスはそのまま（区切り文字のみ正規化）返される。"""
    assert pyfltr.paths.to_cwd_relative("pyfltr/paths.py") == "pyfltr/paths.py"


def test_to_cwd_relative_normalizes_windows_separators() -> None:
    """相対パスのWindows区切りは/へ統一される。"""
    assert pyfltr.paths.to_cwd_relative("pyfltr\\paths.py") == "pyfltr/paths.py"


def test_to_cwd_relative_outside_cwd_keeps_absolute(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cwd配下でない絶対パスは絶対パスのまま返す。"""
    other_root = tmp_path / "outside"
    other_root.mkdir()
    sub_cwd = tmp_path / "inside"
    sub_cwd.mkdir()
    monkeypatch.chdir(sub_cwd)

    outside_abs = other_root / "file.txt"
    assert pyfltr.paths.to_cwd_relative(outside_abs) == outside_abs.as_posix()


def test_to_cwd_relative_outside_cwd_normalizes_separators(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cwd配下でない絶対パスでも区切り文字を`/`へ統一する。"""
    sub_cwd = tmp_path / "inside"
    sub_cwd.mkdir()
    monkeypatch.chdir(sub_cwd)

    outside_abs = f"{tmp_path}/outside\\file.txt"
    expected = f"{tmp_path.as_posix()}/outside/file.txt"
    assert pyfltr.paths.to_cwd_relative(outside_abs) == expected


def test_to_cwd_relative_cwd_returns_dot() -> None:
    """cwd自身は`.`を返す。"""
    assert pyfltr.paths.to_cwd_relative(pathlib.Path.cwd()) == "."


def test_to_cwd_relative_accepts_pathlib_input() -> None:
    """pathlib.Path型の引数を受け付ける。"""
    result = pyfltr.paths.to_cwd_relative(pathlib.Path("pyfltr") / "paths.py")
    assert result == "pyfltr/paths.py"
