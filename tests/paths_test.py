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


@pytest.mark.parametrize(
    "path,path_base_parts,expected",
    [
        # 子プロジェクトのcwd相対の出力は起点相対へ付け替える
        ("tests/x_test.py", ("pkg",), "pkg/tests/x_test.py"),
        # Windows区切りの出力も区切りを`/`へ統一する
        ("tests\\x_test.py", ("pkg",), "pkg/tests/x_test.py"),
        # `..`は字句的に畳み込む
        ("../shared/a.py", ("pkg",), "shared/a.py"),
        # 基準が起点そのものなら値を変えない
        ("src/a.py", (), "src/a.py"),
        # 起点外を指す相対パスは絶対パスで返す（後段で`/`区切りの絶対パスと比較する）
        ("../../outside.py", ("pkg",), None),
        # 空文字と擬似名はパスとして扱わない
        ("", ("pkg",), ""),
        ("<stdin>", ("pkg",), "<stdin>"),
    ],
)
def test_to_start_relative_rebases_relative_output(
    tmp_path: pathlib.Path, path: str, path_base_parts: tuple[str, ...], expected: str | None
) -> None:
    """ツールの実行cwd相対の出力パスを実行起点相対へ変換する。"""
    if "\\" in path and pathlib.PurePath("a\\b").name == "a\\b":
        # POSIXでは`\`をファイル名の一部として扱うため、入力側の区切りを先に統一した値で検証する。
        path = pyfltr.paths.normalize_separators(path)
    actual = pyfltr.paths.to_start_relative(path, path_base=tmp_path.joinpath(*path_base_parts), start_cwd=tmp_path)
    if expected is None:
        assert actual == pyfltr.paths.normalize_separators(tmp_path.parent / "outside.py")
    else:
        assert actual == expected


def test_to_start_relative_keeps_absolute_paths_without_double_prefix(tmp_path: pathlib.Path) -> None:
    """絶対パスの出力は`path_base`と結合せず、起点配下なら起点相対・起点外なら絶対パスで返す。"""
    inside = tmp_path / "pkg" / "a.md"
    outside = tmp_path.parent / "external" / "b.md"

    assert pyfltr.paths.to_start_relative(str(inside), path_base=tmp_path / "pkg", start_cwd=tmp_path) == "pkg/a.md"
    assert pyfltr.paths.to_start_relative(
        str(outside), path_base=tmp_path / "pkg", start_cwd=tmp_path
    ) == pyfltr.paths.normalize_separators(outside)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        # `..`を含む起点内のパスと畳み込み済みの表現
        ("a.md", "./sub/../a.md"),
        # 起点外の相対パスと`/`区切りの絶対パス
        ("../outside/out.md", "<root>/outside/out.md"),
        # 起点配下の絶対パスと相対パス
        ("<root>/proj/a.md", "a.md"),
    ],
)
def test_to_match_key_same_entity_gets_same_key(tmp_path: pathlib.Path, left: str, right: str) -> None:
    """同じ実体を指す異なる表現が同じ突合キーになる。"""
    start = tmp_path / "proj"
    root = tmp_path.as_posix()
    left_key = pyfltr.paths.to_match_key(left.replace("<root>", root), start_cwd=start)
    right_key = pyfltr.paths.to_match_key(right.replace("<root>", root), start_cwd=start)
    assert left_key == right_key


def test_to_match_key_keeps_converted_diagnostic_file(tmp_path: pathlib.Path) -> None:
    """`to_start_relative`で変換済みの診断の`file`は突合キーへ変換しても同じ値になる。"""
    start = tmp_path / "proj"
    for raw in ("sub/../a.md", str(tmp_path / "outside" / "out.md")):
        converted = pyfltr.paths.to_start_relative(raw, path_base=start, start_cwd=start)
        assert pyfltr.paths.to_match_key(converted, start_cwd=start) == converted
