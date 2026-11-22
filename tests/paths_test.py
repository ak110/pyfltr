import os
import pathlib

import pytest

import pyfltr.paths


@pytest.mark.parametrize(
    "path,expected",
    [
        # Windowsパス: バックスラッシュをスラッシュへ変換する
        ("a\\b\\c", "a/b/c"),
        ("a/b/c", "a/b/c"),
        ("a\\b/c", "a/b/c"),
        ("", ""),
        ("foo.py", "foo.py"),
    ],
)
def test_normalize_separators_str(path: str, expected: str) -> None:
    assert pyfltr.paths.normalize_separators(path) == expected


def test_normalize_separators_pathlib() -> None:
    result = pyfltr.paths.normalize_separators(pathlib.Path("a/b/c"))
    assert result == "a/b/c"


def test_to_cwd_relative_converts_absolute_under_cwd() -> None:
    absolute = pathlib.Path.cwd() / "pyfltr" / "paths.py"
    assert pyfltr.paths.to_cwd_relative(absolute) == "pyfltr/paths.py"


def test_to_cwd_relative_keeps_relative_as_is() -> None:
    assert pyfltr.paths.to_cwd_relative("pyfltr/paths.py") == "pyfltr/paths.py"


def test_to_cwd_relative_normalizes_windows_separators() -> None:
    assert pyfltr.paths.to_cwd_relative("pyfltr\\paths.py") == "pyfltr/paths.py"


def test_to_cwd_relative_outside_cwd_keeps_absolute(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    sub_cwd = tmp_path / "inside"
    sub_cwd.mkdir()
    monkeypatch.chdir(sub_cwd)

    outside_abs = f"{tmp_path}/outside\\file.txt"
    expected = f"{tmp_path.as_posix()}/outside/file.txt"
    assert pyfltr.paths.to_cwd_relative(outside_abs) == expected


def test_to_cwd_relative_cwd_returns_dot() -> None:
    assert pyfltr.paths.to_cwd_relative(pathlib.Path.cwd()) == "."


def test_to_cwd_relative_accepts_pathlib_input() -> None:
    result = pyfltr.paths.to_cwd_relative(pathlib.Path("pyfltr") / "paths.py")
    assert result == "pyfltr/paths.py"


@pytest.mark.parametrize(
    "path,path_base_parts,expected",
    [
        ("tests/x_test.py", ("pkg",), "pkg/tests/x_test.py"),
        # Windows区切りの出力も区切りを`/`へ統一する
        ("tests\\x_test.py", ("pkg",), "pkg/tests/x_test.py"),
        ("../shared/a.py", ("pkg",), "shared/a.py"),
        ("src/a.py", (), "src/a.py"),
        ("../../outside.py", ("pkg",), None),
        ("", ("pkg",), ""),
        ("<stdin>", ("pkg",), "<stdin>"),
    ],
)
def test_to_start_relative_rebases_relative_output(
    tmp_path: pathlib.Path, path: str, path_base_parts: tuple[str, ...], expected: str | None
) -> None:
    if "\\" in path and pathlib.PurePath("a\\b").name == "a\\b":
        # POSIXでは`\`をファイル名の一部として扱うため、入力側の区切りを先に統一した値で検証する。
        path = pyfltr.paths.normalize_separators(path)
    actual = pyfltr.paths.to_start_relative(path, path_base=tmp_path.joinpath(*path_base_parts), start_cwd=tmp_path)
    if expected is None:
        assert actual == pyfltr.paths.normalize_separators(tmp_path.parent / "outside.py")
    else:
        assert actual == expected


def test_to_start_relative_keeps_absolute_paths_without_double_prefix(tmp_path: pathlib.Path) -> None:
    inside = tmp_path / "pkg" / "a.md"
    outside = tmp_path.parent / "external" / "b.md"

    assert pyfltr.paths.to_start_relative(str(inside), path_base=tmp_path / "pkg", start_cwd=tmp_path) == "pkg/a.md"
    assert pyfltr.paths.to_start_relative(
        str(outside), path_base=tmp_path / "pkg", start_cwd=tmp_path
    ) == pyfltr.paths.normalize_separators(outside)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a.md", "./sub/../a.md"),
        ("../outside/out.md", "<root>/outside/out.md"),
        ("<root>/proj/a.md", "a.md"),
    ],
)
def test_to_match_key_same_entity_gets_same_key(tmp_path: pathlib.Path, left: str, right: str) -> None:
    start = tmp_path / "proj"
    root = tmp_path.as_posix()
    left_key = pyfltr.paths.to_match_key(left.replace("<root>", root), start_cwd=start)
    right_key = pyfltr.paths.to_match_key(right.replace("<root>", root), start_cwd=start)
    assert left_key == right_key


def test_to_match_key_keeps_converted_diagnostic_file(tmp_path: pathlib.Path) -> None:
    start = tmp_path / "proj"
    for raw in ("sub/../a.md", str(tmp_path / "outside" / "out.md")):
        converted = pyfltr.paths.to_start_relative(raw, path_base=start, start_cwd=start)
        assert pyfltr.paths.to_match_key(converted, start_cwd=start) == converted


def test_is_same_location_absorbs_notation_differences(tmp_path: pathlib.Path) -> None:
    (tmp_path / "proj" / "sub").mkdir(parents=True)
    assert pyfltr.paths.is_same_location(tmp_path / "proj", f"{tmp_path / 'proj' / 'sub'}{os.sep}..{os.sep}")


def test_is_same_location_distinguishes_other_directory(tmp_path: pathlib.Path) -> None:
    assert not pyfltr.paths.is_same_location(tmp_path / "proj", tmp_path / "proj2")
