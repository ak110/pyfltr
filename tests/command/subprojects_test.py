from __future__ import annotations

import pathlib
import typing

import pytest

import pyfltr.command.subprojects
import pyfltr.config.config


def _make_pyproject(path: pathlib.Path, name: str = "dummy") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")


@pytest.mark.parametrize(
    "layout,expected_count,expected_relatives",
    [
        ({"root": True}, 1, ["."]),
        ({"root": True, "subs": ["a", "b"]}, 3, [".", "a", "b"]),
        ({"root": True, "subs": ["a", "a/inner"]}, 3, [".", "a", "a/inner"]),
        ({"root": False}, 0, []),
    ],
)
def test_discover_subprojects_layouts(
    tmp_path: pathlib.Path,
    layout: dict[str, typing.Any],
    expected_count: int,
    expected_relatives: list[str],
) -> None:
    if layout.get("root"):
        _make_pyproject(tmp_path)
    subs_paths: list[str] = list(layout.get("subs", []))
    for sub_rel in subs_paths:
        _make_pyproject(tmp_path / sub_rel)
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(
        tmp_path,
        config,
        git_check_ignore=lambda _start, _candidates: set(),
    )
    assert len(subs) == expected_count
    assert [s.relative for s in subs] == expected_relatives


def test_discover_subprojects_excludes_blacklisted_dirs(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path)
    _make_pyproject(tmp_path / ".venv" / "lib_pkg")
    _make_pyproject(tmp_path / "node_modules" / "pkg")
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert [s.relative for s in subs] == ["."]


def test_discover_subprojects_extra_excludes(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path)
    _make_pyproject(tmp_path / "ignored_pkg")
    config = pyfltr.config.config.create_default_config()
    config.values["subproject-exclude"] = ["ignored_pkg"]
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert [s.relative for s in subs] == ["."]


def test_discover_subprojects_skips_file_expansion_excludes(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "pkg" / "example")
    _make_pyproject(tmp_path / "venv" / "lib" / "python3.12" / "site-packages" / "pkg" / "example2")
    _make_pyproject(tmp_path / ".tox" / "py312" / "lib" / "python3.12" / "site-packages" / "pkg" / "example")
    _make_pyproject(tmp_path / "vendor" / "libs" / "one")
    _make_pyproject(tmp_path / "vendor" / "libs" / "two")
    config = pyfltr.config.config.create_default_config()
    assert "venv" in config.values["exclude"]
    assert ".tox" in config.values["exclude"]
    config.values["extend-exclude"] = ["vendor/libs"]
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert [s.relative for s in subs] == []


def test_classify_files_by_subproject_deep_first(tmp_path: pathlib.Path) -> None:
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

    result, external = pyfltr.command.subprojects.classify_files_by_subproject(
        [parent_file, child_file, root_file], subs, tmp_path
    )
    root_cwd = next(s.cwd for s in subs if s.relative == ".")
    a_cwd = next(s.cwd for s in subs if s.relative == "a")
    inner_cwd = next(s.cwd for s in subs if s.relative == "a/inner")
    assert result[root_cwd] == [root_file]
    assert result[a_cwd] == [parent_file]
    assert result[inner_cwd] == [child_file]
    assert not external


def test_classify_files_by_subproject_unassigned_files_go_to_start_cwd(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path / "a")
    _make_pyproject(tmp_path / "b")
    _make_pyproject(tmp_path / "excluded_pkg")
    config = pyfltr.config.config.create_default_config()
    config.values["subproject-exclude"] = ["excluded_pkg"]
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert [s.relative for s in subs] == ["a", "b"]

    root_file = pathlib.Path("sample.md")
    excluded_file = pathlib.Path("excluded_pkg") / "doc.md"
    sub_file = pathlib.Path("a") / "doc.md"
    outside_dir = tmp_path.parent / f"outside-{tmp_path.name}"
    outside_dir.mkdir()
    outside_file = (outside_dir / "out.md").resolve()
    for path in (tmp_path / root_file, tmp_path / excluded_file, tmp_path / sub_file, outside_file):
        path.write_text("# doc\n", encoding="utf-8")

    result, external = pyfltr.command.subprojects.classify_files_by_subproject(
        [root_file, excluded_file, sub_file, outside_file], subs, tmp_path
    )
    assert result[tmp_path.resolve()] == [root_file, excluded_file]
    assert result[(tmp_path / "a").resolve()] == [sub_file]
    assert external == [outside_file]


def test_find_uv_lock_for_cwd_direct(tmp_path: pathlib.Path) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(tmp_path)
    assert result == tmp_path / "uv.lock"


def test_find_uv_lock_for_cwd_workspace_parent_search(tmp_path: pathlib.Path) -> None:
    workspace_root = tmp_path / "workspace"
    member = workspace_root / "crates" / "pkg"
    member.mkdir(parents=True)
    (workspace_root / "uv.lock").write_text("", encoding="utf-8")
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(member, workspace_root=workspace_root)
    assert result == workspace_root / "uv.lock"


def test_find_uv_lock_for_cwd_not_found(tmp_path: pathlib.Path) -> None:
    workspace_root = tmp_path / "workspace"
    member = workspace_root / "crates" / "pkg"
    member.mkdir(parents=True)
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(member, workspace_root=workspace_root)
    assert result is None


def test_find_uv_lock_for_cwd_does_not_cross_workspace_root(tmp_path: pathlib.Path) -> None:
    workspace_root = tmp_path / "workspace"
    member = workspace_root / "pkg"
    member.mkdir(parents=True)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    result = pyfltr.command.subprojects.find_uv_lock_for_cwd(member, workspace_root=workspace_root)
    assert result is None


def test_uv_workspace_members_glob_expansion(tmp_path: pathlib.Path) -> None:
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


@pytest.mark.parametrize("marker_name", ["MyApp.csproj", "MySolution.sln"])
def test_is_subproject_dir_dotnet_marker_only(tmp_path: pathlib.Path, marker_name: str) -> None:
    assert not pyfltr.command.subprojects.is_subproject_dir(tmp_path)
    (tmp_path / marker_name).write_text("", encoding="utf-8")
    assert pyfltr.command.subprojects.is_subproject_dir(tmp_path)


def test_is_subproject_dir_package_json_only_not_detected(tmp_path: pathlib.Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert not pyfltr.command.subprojects.is_subproject_dir(tmp_path)


def test_discover_subprojects_hybrid_cargo_toml(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path)
    (tmp_path / "rust" / "crate_a").mkdir(parents=True)
    (tmp_path / "rust" / "crate_a" / "Cargo.toml").write_text(
        '[package]\nname = "crate_a"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert {s.relative for s in subs} == {".", "rust/crate_a"}


def test_discover_subprojects_hybrid_dotnet_csproj(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path)
    (tmp_path / "dotnet" / "app_a").mkdir(parents=True)
    (tmp_path / "dotnet" / "app_a" / "AppA.csproj").write_text("", encoding="utf-8")
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert {s.relative for s in subs} == {".", "dotnet/app_a"}


def test_discover_subprojects_cargo_workspace_excludes_registered_members(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path)
    ws = tmp_path / "rust"
    ws.mkdir()
    (ws / "Cargo.toml").write_text('[workspace]\nmembers = ["crate_a"]\nexclude = []\n', encoding="utf-8")
    (ws / "crate_a").mkdir()
    (ws / "crate_a" / "Cargo.toml").write_text('[package]\nname = "crate_a"\nversion = "0.1.0"\n', encoding="utf-8")
    (ws / "orphan").mkdir()
    (ws / "orphan" / "Cargo.toml").write_text('[package]\nname = "orphan"\nversion = "0.1.0"\n', encoding="utf-8")
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert {s.relative for s in subs} == {".", "rust", "rust/orphan"}


def test_discover_subprojects_dotnet_solution_excludes_registered_projects(tmp_path: pathlib.Path) -> None:
    _make_pyproject(tmp_path)
    sln_dir = tmp_path / "dotnet"
    sln_dir.mkdir()
    (sln_dir / "MySolution.sln").write_text(
        'Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "AppA", "app_a/AppA.csproj", '
        '"{11111111-1111-1111-1111-111111111111}"\nEndProject\n',
        encoding="utf-8",
    )
    (sln_dir / "app_a").mkdir()
    (sln_dir / "app_a" / "AppA.csproj").write_text("", encoding="utf-8")
    (sln_dir / "orphan").mkdir()
    (sln_dir / "orphan" / "OrphanApp.csproj").write_text("", encoding="utf-8")
    config = pyfltr.config.config.create_default_config()
    subs = pyfltr.command.subprojects.discover_subprojects(tmp_path, config, git_check_ignore=lambda _start, _candidates: set())
    assert {s.relative for s in subs} == {".", "dotnet", "dotnet/orphan"}
