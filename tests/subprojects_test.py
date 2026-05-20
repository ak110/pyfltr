"""サブプロジェクト検出・分類・uv.lock親探索のテスト。"""

from __future__ import annotations

import pathlib
import typing

import pytest

import pyfltr.command.subprojects
import pyfltr.config.config


def _make_pyproject(path: pathlib.Path, name: str = "dummy") -> None:
    """テスト用の pyproject.toml を作成する。"""
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")


@pytest.mark.parametrize(
    "layout,expected_count,expected_relatives",
    [
        # 単一プロジェクト
        ({"root": True}, 1, ["."]),
        # ルート + 2サブプロジェクト
        ({"root": True, "subs": ["a", "b"]}, 3, [".", "a", "b"]),
        # ネスト構成（親 + 子）
        ({"root": True, "subs": ["a", "a/inner"]}, 3, [".", "a", "a/inner"]),
        # 検出0件（ルートに pyproject.toml なし）
        ({"root": False}, 0, []),
    ],
)
def test_discover_subprojects_layouts(
    tmp_path: pathlib.Path,
    layout: dict[str, typing.Any],
    expected_count: int,
    expected_relatives: list[str],
) -> None:
    """各レイアウトでサブプロジェクト一覧が期待通り検出されることを確認する。"""
    if layout.get("root"):
        _make_pyproject(tmp_path)
    subs_paths: list[str] = list(layout.get("subs", []))
    for sub_rel in subs_paths:
        _make_pyproject(tmp_path / sub_rel)
    config = pyfltr.config.config.create_default_config()
    # gitignore 判定はテスト環境で git not-a-repo になるため空集合フックを渡す。
    subs = pyfltr.command.subprojects.discover_subprojects(
        tmp_path,
        config,
        git_check_ignore=lambda _start, _candidates: set(),
    )
    assert len(subs) == expected_count
    assert [s.relative for s in subs] == expected_relatives


def test_discover_subprojects_excludes_blacklisted_dirs(tmp_path: pathlib.Path) -> None:
    """`.venv`・`node_modules` 配下の pyproject.toml は検出されない。"""
    _make_pyproject(tmp_path)
    _make_pyproject(tmp_path / ".venv" / "lib_pkg")
    _make_pyproject(tmp_path / "node_modules" / "pkg")
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert [s.relative for s in subs] == ["."]


def test_discover_subprojects_extra_excludes(tmp_path: pathlib.Path) -> None:
    """`subproject-exclude` で指定したディレクトリ名は走査から除外する。"""
    _make_pyproject(tmp_path)
    _make_pyproject(tmp_path / "ignored_pkg")
    config = pyfltr.config.config.create_default_config()
    config.values["subproject-exclude"] = ["ignored_pkg"]
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert [s.relative for s in subs] == ["."]


def test_classify_files_by_subproject_deep_first(tmp_path: pathlib.Path) -> None:
    """最深一致でファイルをサブプロジェクトに割り当てる（親と子の境界）。"""
    _make_pyproject(tmp_path)
    _make_pyproject(tmp_path / "a")
    _make_pyproject(tmp_path / "a" / "inner")
    parent_file = pathlib.Path("a") / "foo.py"
    child_file = pathlib.Path("a") / "inner" / "foo.py"
    root_file = pathlib.Path("root.py")
    (tmp_path / parent_file).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / parent_file).write_text("", encoding="utf-8")
    (tmp_path / child_file).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / child_file).write_text("", encoding="utf-8")
    (tmp_path / root_file).write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert {s.relative for s in subs} == {".", "a", "a/inner"}

    result = pyfltr.command.subprojects.classify_files_by_subproject([parent_file, child_file, root_file], subs, tmp_path)
    # 最深一致でルート (./root.py)、a/foo.py は a、a/inner/foo.py は a/inner へ。
    root_cwd = next(s.cwd for s in subs if s.relative == ".")
    a_cwd = next(s.cwd for s in subs if s.relative == "a")
    inner_cwd = next(s.cwd for s in subs if s.relative == "a/inner")
    assert result[root_cwd] == [root_file]
    assert result[a_cwd] == [parent_file]
    assert result[inner_cwd] == [child_file]


def test_find_uv_lock_for_cwd_direct(tmp_path: pathlib.Path) -> None:
    """cwd 直下に `uv.lock` があれば返す。"""
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(tmp_path)
    assert result == tmp_path / "uv.lock"


def test_find_uv_lock_for_cwd_workspace_parent_search(tmp_path: pathlib.Path) -> None:
    """workspace member の cwd で `uv.lock` を親方向探索する。"""
    workspace_root = tmp_path / "workspace"
    member = workspace_root / "crates" / "pkg"
    member.mkdir(parents=True)
    (workspace_root / "uv.lock").write_text("", encoding="utf-8")
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(member, workspace_root=workspace_root)
    assert result == workspace_root / "uv.lock"


def test_find_uv_lock_for_cwd_not_found(tmp_path: pathlib.Path) -> None:
    """`uv.lock` が見つからなければ `None` を返す。"""
    workspace_root = tmp_path / "workspace"
    member = workspace_root / "crates" / "pkg"
    member.mkdir(parents=True)
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(member, workspace_root=workspace_root)
    assert result is None


def test_find_uv_lock_for_cwd_does_not_cross_workspace_root(tmp_path: pathlib.Path) -> None:
    """workspace root を越えて祖先方向へ探索しない。"""
    workspace_root = tmp_path / "workspace"
    member = workspace_root / "pkg"
    member.mkdir(parents=True)
    # workspace root の外側に uv.lock を置いても拾わない。
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(member, workspace_root=workspace_root)
    assert result is None


def test_uv_workspace_members_glob_expansion(tmp_path: pathlib.Path) -> None:
    """`[tool.uv.workspace] members` glob で member を検出する。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.uv.workspace]\nmembers = ["crates/*"]\n',
        encoding="utf-8",
    )
    _make_pyproject(tmp_path / "crates" / "alpha")
    _make_pyproject(tmp_path / "crates" / "beta")
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert {s.relative for s in subs} == {".", "crates/alpha", "crates/beta"}
    members = [s for s in subs if s.uv_workspace_root is not None]
    assert {s.relative for s in members} == {"crates/alpha", "crates/beta"}


def test_is_subproject_dir(tmp_path: pathlib.Path) -> None:
    """`pyproject.toml` の有無で判定する。"""
    assert not pyfltr.command.subprojects.is_subproject_dir(tmp_path)
    _make_pyproject(tmp_path)
    assert pyfltr.command.subprojects.is_subproject_dir(tmp_path)
