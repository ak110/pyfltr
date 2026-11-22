import collections.abc
import logging
import os
import pathlib
import subprocess
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_


@pytest.fixture(name="_git_repo")
def _git_repo_fixture(tmp_path: pathlib.Path) -> collections.abc.Generator[pathlib.Path]:
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    subprocess.run(["git", "init"], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
    yield tmp_path
    os.chdir(original_cwd)


def test_filter_by_changed_since_normal(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    committed = tmp_path / "committed.py"
    committed.write_text("x = 1\n")
    unchanged = tmp_path / "unchanged.py"
    unchanged.write_text("y = 2\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    changed = tmp_path / "changed.py"
    changed.write_text("z = 3\n")
    subprocess.run(["git", "add", str(changed)], check=True, capture_output=True)

    all_files = [
        pathlib.Path("committed.py"),
        pathlib.Path("unchanged.py"),
        pathlib.Path("changed.py"),
    ]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD")

    assert result == [pathlib.Path("changed.py")]
    assert not pyfltr.warnings_.collected_warnings()


def test_filter_by_changed_since_empty_diff(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    initial = tmp_path / "initial.py"
    initial.write_text("x = 1\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    all_files = [pathlib.Path("initial.py")]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD")

    assert result == []
    assert not pyfltr.warnings_.collected_warnings()


def test_filter_by_changed_since_invalid_ref(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    initial = tmp_path / "a.py"
    initial.write_text("x = 1\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    all_files = [pathlib.Path("a.py")]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "no-such-branch-xyz")

    assert result == all_files
    warnings = pyfltr.warnings_.collected_warnings()
    assert len(warnings) == 1
    assert warnings[0]["source"] == "changed-since"
    assert "no-such-branch-xyz" in warnings[0]["message"]


def test_filter_by_changed_since_from_subdirectory_of_git_root(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "a.md").write_text("# a\n")
    (proj / "b.md").write_text("# b\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)
    (proj / "a.md").write_text("# a  \n")

    all_files = [pathlib.Path("a.md"), pathlib.Path("b.md")]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD", cwd=proj)

    assert result == [pathlib.Path("a.md")]


def test_filter_by_changed_since_git_not_found(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))

    all_files = [pathlib.Path("a.py")]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD")

    assert result == all_files
    warnings = pyfltr.warnings_.collected_warnings()
    assert len(warnings) == 1
    assert warnings[0]["source"] == "changed-since"
    assert "git が見つからない" in warnings[0]["message"]


def test_filter_by_changed_since_excludes_untracked(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    tracked = tmp_path / "tracked.py"
    tracked.write_text("x = 1\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    tracked.write_text("x = 2\n")
    subprocess.run(["git", "add", str(tracked)], check=True, capture_output=True)

    untracked = tmp_path / "untracked.py"
    untracked.write_text("y = 3\n")

    all_files = [
        pathlib.Path("tracked.py"),
        pathlib.Path("untracked.py"),
    ]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD")

    assert result == [pathlib.Path("tracked.py")]
    assert not pyfltr.warnings_.collected_warnings()


def test_excluded_default_patterns() -> None:
    config = pyfltr.config.config.create_default_config()

    assert pyfltr.command.targets.excluded(pathlib.Path(".serena"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".cursor"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".idea"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".venv"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path("node_modules"), config)

    assert pyfltr.command.targets.excluded(pathlib.Path(".serena/memories/foo.md"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".cursor/rules/bar.mdc"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".idea/workspace.xml"), config)

    assert pyfltr.command.targets.excluded(pathlib.Path(".aider.conf.yml"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".aider.chat.history.md"), config)

    assert not pyfltr.command.targets.excluded(pathlib.Path("pyfltr/config.py"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path("tests/command_test.py"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path("README.md"), config)

    config.values["exclude"] = []
    config.values["extend-exclude"] = ["sample.py"]
    assert pyfltr.command.targets.excluded(pathlib.Path("sample.py"), config) == ("extend-exclude", "sample.py")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        pytest.param("pnpm-lock.yaml", True, id="pnpm-lock.yaml"),
        pytest.param("yarn.lock", True, id="yarn.lock"),
        pytest.param("uv.lock", True, id="uv.lock"),
        pytest.param("aube-lock.yaml", True, id="aube-lock.yaml"),
        pytest.param(".mise/locks/npm-pnpm/12.17.0/aube-lock.yaml", True, id="nested-aube-lock.yaml"),
        pytest.param("npm-shrinkwrap.json", True, id="npm-shrinkwrap.json"),
        pytest.param(".mise/config.toml", False, id="mise-config.toml"),
        pytest.param("app.min.js", True, id="app.min.js"),
        pytest.param("style.min.css", True, id="style.min.css"),
        pytest.param("app.js.map", True, id="app.js.map"),
        pytest.param("Cargo.lock", False, id="Cargo.lock"),
        pytest.param("config.yaml", False, id="config.yaml"),
        pytest.param("app.js", False, id="app.js"),
        pytest.param("map.txt", False, id="map.txt"),
    ],
)
def test_excluded_added_default_patterns(name: str, expected: bool) -> None:
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.targets.excluded(pathlib.Path(name), config)
    if expected:
        assert result
    else:
        assert not result


def test_excluded_disabled_by_empty_config() -> None:
    config = pyfltr.config.config.create_default_config()
    config.values["exclude"] = []
    config.values["extend-exclude"] = []

    assert not pyfltr.command.targets.excluded(pathlib.Path(".venv"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path("node_modules"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path(".serena/memories/foo.md"), config)


def test_expand_all_files_reuses_shared_parent_exclude_match(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "a.py").write_text("", encoding="utf-8")
    (shared / "b.py").write_text("", encoding="utf-8")
    config = pyfltr.config.config.create_default_config()
    config.values["exclude"] = ["never-match"]
    config.values["extend-exclude"] = []
    config.values["respect-gitignore"] = False
    calls: dict[pathlib.Path, int] = {}
    original_match = pathlib.Path.match

    def counting_match(path: pathlib.Path, pattern: str) -> bool:
        calls[path] = calls.get(path, 0) + 1
        return original_match(path, pattern)

    monkeypatch.setattr(pathlib.Path, "match", counting_match)

    result = pyfltr.command.targets.expand_all_files([pathlib.Path("shared")], config, start_cwd=tmp_path)

    assert result == [pathlib.Path("shared/a.py"), pathlib.Path("shared/b.py")]
    assert calls[pathlib.Path("shared")] == 1


def test_expand_all_files_does_not_reuse_exclude_cache_between_configs(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("sample\n", encoding="utf-8")
    config = pyfltr.config.config.create_default_config()
    config.values["extend-exclude"] = []
    config.values["respect-gitignore"] = False
    config.values["exclude"] = ["*.txt"]

    excluded_result = pyfltr.command.targets.expand_all_files([pathlib.Path("sample.txt")], config, start_cwd=tmp_path)
    config.values["exclude"] = []
    included_result = pyfltr.command.targets.expand_all_files([pathlib.Path("sample.txt")], config, start_cwd=tmp_path)

    assert excluded_result == []
    assert included_result == [pathlib.Path("sample.txt")]


def test_expand_all_files_collapses_dot_dot_in_relative_targets(tmp_path: pathlib.Path) -> None:
    start = tmp_path / "proj"
    (start / "sub").mkdir(parents=True)
    (start / "build").mkdir()
    (start / "a.md").write_text("# a\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "out.md").write_text("# out\n")
    config = pyfltr.config.config.create_default_config()
    config.values["respect-gitignore"] = False

    result = pyfltr.command.targets.expand_all_files(
        [pathlib.Path("sub/../a.md"), pathlib.Path("build/../a.md"), pathlib.Path("../outside/out.md")],
        config,
        start_cwd=start,
    )

    assert [p.as_posix() for p in result] == ["../outside/out.md", "a.md"]
    assert pyfltr.warnings_.filtered_direct_files(reason="excluded") == []


def test_expand_all_files_respects_gitignore(tmp_path: pathlib.Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("x = 2\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        result = pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in result}
        assert "main.py" in names
        assert "ignored.py" not in names
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_gitignore_disabled(tmp_path: pathlib.Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("x = 2\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        config.values["respect-gitignore"] = False
        all_files = pyfltr.command.targets.expand_all_files([], config)
        result = pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in result}
        assert "main.py" in names
        assert "ignored.py" in names
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_excludes_generated_lock_files(tmp_path: pathlib.Path) -> None:
    lock_dir = tmp_path / ".mise" / "locks" / "npm-pnpm" / "12.17.0"
    lock_dir.mkdir(parents=True)
    (lock_dir / "aube-lock.yaml").write_text("lockfileVersion: 1\n")
    (tmp_path / "npm-shrinkwrap.json").write_text("{}\n")
    (tmp_path / ".mise" / "config.toml").write_text("[tools]\n")
    (tmp_path / "config.yaml").write_text("a: 1\n")

    config = pyfltr.config.config.create_default_config()
    all_files = pyfltr.command.targets.expand_all_files([], config, start_cwd=tmp_path)
    paths = {p.as_posix() for p in all_files}
    assert ".mise/locks/npm-pnpm/12.17.0/aube-lock.yaml" not in paths
    assert "npm-shrinkwrap.json" not in paths
    assert ".mise/config.toml" in paths
    assert "config.yaml" in paths


def test_expand_all_files_no_git_repo(tmp_path: pathlib.Path) -> None:
    (tmp_path / "main.py").write_text("x = 1\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        result = pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in result}
        assert "main.py" in names
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_warns_excluded_file(tmp_path: pathlib.Path, caplog) -> None:
    target = pathlib.Path(r"nested\sample.py")
    filesystem_target = tmp_path / target
    filesystem_target.parent.mkdir(parents=True, exist_ok=True)
    filesystem_target.write_text("x = 1\n")
    normalized_target = pyfltr.paths.normalize_separators(target)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        pyfltr.warnings_.clear()
        config.values["extend-exclude"] = ["*sample.py"]
        with caplog.at_level(logging.WARNING):
            result = pyfltr.command.targets.expand_all_files([target], config)
        assert len(result) == 0
        assert "除外設定により無視されました" in caplog.text
        assert normalized_target in caplog.text
        assert "対処: 除外設定を無視して検査する場合は `--no-exclude`" in caplog.text
        assert pyfltr.warnings_.filtered_direct_files(reason="excluded") == [normalized_target]
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_warns_io_error(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    target = pathlib.Path(r"nested\unreadable")
    filesystem_target = tmp_path / target
    filesystem_target.mkdir(parents=True)
    normalized_target = pyfltr.paths.normalize_separators(target)
    original_iterdir = pathlib.Path.iterdir

    def _raise_for_target(path: pathlib.Path) -> typing.Iterator[pathlib.Path]:
        if path == filesystem_target:
            raise OSError("test I/O error")
        return original_iterdir(path)

    monkeypatch.setattr(pathlib.Path, "iterdir", _raise_for_target)
    config = pyfltr.config.config.create_default_config()
    config.values["respect-gitignore"] = False

    with caplog.at_level(logging.WARNING):
        result = pyfltr.command.targets.expand_all_files([target], config, start_cwd=tmp_path)

    assert result == []
    assert f"指定されたパスを読み取れないため対象から除外しました: {normalized_target}: test I/O error" in caplog.text
    assert "読み取り権限を確認してください" in caplog.text


def test_expand_all_files_warns_missing_file(tmp_path: pathlib.Path, caplog) -> None:
    target = pathlib.Path(r"nested\does_not_exist.py")
    normalized_target = pyfltr.paths.normalize_separators(target)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        pyfltr.warnings_.clear()
        config = pyfltr.config.config.create_default_config()
        with caplog.at_level(logging.WARNING):
            result = pyfltr.command.targets.expand_all_files([target], config)
        assert len(result) == 0
        assert "指定されたパスが見つかりません" in caplog.text
        assert normalized_target in caplog.text
        assert pyfltr.warnings_.filtered_direct_files(reason="missing") == [normalized_target]
        assert not pyfltr.warnings_.filtered_direct_files(reason="excluded")
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_warns_gitignored_file(tmp_path: pathlib.Path, caplog) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    target = pathlib.Path(r"nested\ignored.py")
    filesystem_target = tmp_path / target
    filesystem_target.parent.mkdir(parents=True, exist_ok=True)
    filesystem_target.write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("*ignored.py\n")
    normalized_target = pyfltr.paths.normalize_separators(target)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        with caplog.at_level(logging.WARNING):
            result = pyfltr.command.targets.expand_all_files([target], config)
        assert len(result) == 0
        assert ".gitignore により無視されました" in caplog.text
        assert normalized_target in caplog.text
        assert pyfltr.warnings_.filtered_direct_files(reason="excluded") == [normalized_target]
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_filters_symlinked_files_against_gitignore(tmp_path: pathlib.Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "ok.py").write_text("x = 1\n")
    (tmp_path / "real" / "ignored___.py").write_text("y = 2\n")
    (tmp_path / "link").symlink_to("real", target_is_directory=True)
    (tmp_path / ".gitignore").write_text("*___*\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        py_files = pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in py_files}
        assert "ok.py" in names
        assert "ignored___.py" not in names
    finally:
        os.chdir(original_cwd)


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [("true\n", "inside"), ("false\n", "outside")],
)
def test_work_tree_state_classifies_successful_git_result(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    expected: str,
) -> None:
    result = subprocess.CompletedProcess(
        args=["git", "rev-parse", "--is-inside-work-tree"],
        returncode=0,
        stdout=stdout,
        stderr="",
    )
    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", lambda *args, **kwargs: result)

    assert pyfltr.command.targets._work_tree_state(tmp_path) == expected  # pylint: disable=protected-access  # 分類境界を直接検証する


def test_work_tree_state_returns_unknown_on_timeout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    def timeout_run(args: list[str], **kwargs: typing.Any) -> typing.NoReturn:
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", timeout_run)

    assert pyfltr.command.targets._work_tree_state(tmp_path) == "unknown"  # pylint: disable=protected-access  # タイムアウト時の分類を直接検証する


def test_work_tree_state_returns_unknown_on_git_dir_probe_timeout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_dir = tmp_path / "git-dir"
    git_dir.mkdir()
    monkeypatch.setenv("GIT_DIR", str(git_dir))
    main_result = subprocess.CompletedProcess(
        args=["git", "rev-parse", "--is-inside-work-tree"],
        returncode=128,
        stdout="",
        stderr="fatal: not a git repository\n",
    )

    def timeout_probe(args: list[str], **kwargs: typing.Any) -> typing.Any:
        if args == ["git", "rev-parse", "--is-inside-work-tree"]:
            return main_result
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", timeout_probe)

    assert pyfltr.command.targets._work_tree_state(tmp_path) == "unknown"  # pylint: disable=protected-access  # 解決確認のタイムアウト分類を直接検証する


def test_expand_all_files_warns_on_check_ignore_failure(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("y = 2\n")

    fake_result = subprocess.CompletedProcess(
        args=["git", "check-ignore"],
        returncode=128,
        stdout="ignored.py\0",
        stderr="fatal: pathspec '...' is beyond a symbolic link\n",
    )
    original_run = subprocess.run

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        if isinstance(args, list) and args[:2] == ["git", "check-ignore"] and "--stdin" in args:
            return fake_result
        return original_run(args, **kwargs)  # pylint: disable=subprocess-run-check  # 呼び出し元のkwargsを転送

    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", fake_run)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        pyfltr.warnings_.clear()
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        names = {p.name for p in pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])}
        assert "ok.py" in names
        assert "ignored.py" not in names
        collected = pyfltr.warnings_.collected_warnings()
        assert any("git check-ignore" in w["message"] and "128" in w["message"] for w in collected)
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_keeps_files_outside_git_worktree(tmp_path: pathlib.Path) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "other.py").write_text("y = 2\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        pyfltr.warnings_.clear()
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        names = {p.name for p in pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])}
        git_warnings = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "git"]
        assert not git_warnings
        assert names == {"ok.py", "other.py"}
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_warns_on_unexpected_check_ignore_failure_outside_worktree(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("y = 2\n")
    fake_result = subprocess.CompletedProcess(
        args=["git", "check-ignore"],
        returncode=2,
        stdout="ignored.py\0",
        stderr="fatal: unexpected failure\n",
    )
    original_run = subprocess.run

    def fake_run(args: list[str], **kwargs: typing.Any) -> typing.Any:
        if args[:2] == ["git", "check-ignore"] and "--stdin" in args:
            return fake_result
        kwargs.pop("check", None)
        return original_run(args, check=False, **kwargs)

    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", fake_run)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        pyfltr.warnings_.clear()
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        names = {p.name for p in pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])}
        collected = pyfltr.warnings_.collected_warnings()
        assert "ok.py" in names
        assert "ignored.py" not in names
        assert any(w["source"] == "git" and "2" in w["message"] for w in collected)
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def _mock_git_ignore_failure(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    check_ignore_result = subprocess.CompletedProcess(
        args=["git", "check-ignore"], returncode=128, stdout="ignored.py\0", stderr=stderr
    )
    rev_parse_result = subprocess.CompletedProcess(
        args=["git", "rev-parse", "--is-inside-work-tree"], returncode=128, stdout="", stderr=stderr
    )
    original_run = subprocess.run

    def fake_run(args: list[str], **kwargs: typing.Any) -> typing.Any:
        if args[:2] == ["git", "check-ignore"] and "--stdin" in args:
            return check_ignore_result
        if args == ["git", "rev-parse", "--is-inside-work-tree"]:
            return rev_parse_result
        kwargs.pop("check", None)
        return original_run(args, check=False, **kwargs)

    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", fake_run)


def _expand_python_names(cwd: pathlib.Path) -> tuple[set[str], list[dict[str, typing.Any]]]:
    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(cwd)
        pyfltr.warnings_.clear()
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        names = {path.name for path in pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])}
        return names, pyfltr.warnings_.collected_warnings()
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_warns_when_git_worktree_state_is_unknown(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("y = 2\n")
    _mock_git_ignore_failure(monkeypatch, "fatal: detected dubious ownership\n")

    names, collected = _expand_python_names(tmp_path)

    assert "ok.py" in names
    assert "ignored.py" not in names
    assert any(w["source"] == "git" and "128" in w["message"] for w in collected)


def test_expand_all_files_warns_when_git_env_overrides_worktree_state(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "ok.py").write_text("x = 1\n")
    (worktree / "ignored.py").write_text("y = 2\n")
    bad_config = tmp_path / "bad.gitconfig"
    bad_config.write_text("[broken\n")
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(worktree))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(bad_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    check_ignore_result = subprocess.CompletedProcess(
        args=["git", "check-ignore"],
        returncode=128,
        stdout="ignored.py\0",
        stderr="fatal: bad config\n",
    )
    rev_parse_result = subprocess.CompletedProcess(
        args=["git", "rev-parse", "--is-inside-work-tree"],
        returncode=128,
        stdout="",
        stderr="fatal: bad config\n",
    )
    original_run = subprocess.run

    def fake_run(args: list[str], **kwargs: typing.Any) -> typing.Any:
        if args[:2] == ["git", "check-ignore"] and "--stdin" in args:
            return check_ignore_result
        if args == ["git", "rev-parse", "--is-inside-work-tree"]:
            return rev_parse_result
        kwargs.pop("check", None)
        return original_run(args, check=False, **kwargs)

    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", fake_run)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(worktree)
        pyfltr.warnings_.clear()
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        names = {p.name for p in pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])}
        collected = pyfltr.warnings_.collected_warnings()
        assert "ok.py" in names
        assert "ignored.py" not in names
        assert any(w["source"] == "git" and "128" in w["message"] for w in collected)
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_keeps_files_with_git_work_tree_only(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("y = 2\n")
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    _mock_git_ignore_failure(monkeypatch, "fatal: not a git repository\n")

    names, collected = _expand_python_names(tmp_path)

    assert names == {"ok.py", "ignored.py"}
    assert not [w for w in collected if w["source"] == "git"]


def test_expand_all_files_keeps_files_with_invalid_git_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "ok.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("y = 2\n")
    invalid_git_dir = tmp_path / "not-a-git-dir"
    invalid_git_dir.mkdir()
    monkeypatch.setenv("GIT_DIR", str(invalid_git_dir))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))
    _mock_git_ignore_failure(monkeypatch, "fatal: not a git repository\n")

    names, collected = _expand_python_names(tmp_path)

    assert names == {"ok.py", "ignored.py"}
    assert not [w for w in collected if w["source"] == "git"]


def test_expand_all_files_keeps_outside_repo_symlink_target(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    (repo / "in_repo.py").write_text("x = 1\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.py").write_text("y = 2\n")
    (repo / "extlink").symlink_to(outside, target_is_directory=True)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(repo)
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        names = {p.name for p in pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])}
        assert "in_repo.py" in names
        assert "external.py" in names
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_skips_gitignored_symlink_dir(tmp_path: pathlib.Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "child.py").write_text("x = 1\n")
    (tmp_path / "ignored_link").symlink_to("real", target_is_directory=True)
    (tmp_path / "regular.py").write_text("y = 2\n")
    (tmp_path / ".gitignore").write_text("ignored_link\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        py_files = pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in py_files}
        assert "regular.py" in names
        assert not any("ignored_link" in str(p) for p in py_files)
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_deduplicates_realpath(tmp_path: pathlib.Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "shared.py").write_text("x = 1\n")
    (tmp_path / "alias").symlink_to("real", target_is_directory=True)
    (tmp_path / "another.py").write_text("y = 2\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        config.values["respect-gitignore"] = False
        all_files = pyfltr.command.targets.expand_all_files([], config)
        py_files = pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])
        shared_paths = [p for p in py_files if p.name == "shared.py"]
        assert len(shared_paths) == 1
        assert py_files == sorted(py_files, key=str)
    finally:
        os.chdir(original_cwd)


@pytest.mark.parametrize(
    ("symlink_names", "real_name"),
    [
        (["AAA.md"], "bbb.md"),
        (["zzz.md"], "bbb.md"),
        (["AAA.md", "zzz.md"], "bbb.md"),
    ],
)
def test_expand_all_files_prefers_non_symlink_target(tmp_path: pathlib.Path, symlink_names: list[str], real_name: str) -> None:
    for name in symlink_names:
        (tmp_path / name).symlink_to(real_name)
    (tmp_path / real_name).write_text("x\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        config.values["respect-gitignore"] = False
        all_files = pyfltr.command.targets.expand_all_files([], config)
        md_files = pyfltr.command.targets.filter_by_globs(all_files, ["*.md"])
        assert md_files == [pathlib.Path(real_name)]
    finally:
        os.chdir(original_cwd)


def test_filter_by_globs() -> None:
    files = [
        pathlib.Path("main.py"),
        pathlib.Path("test_main.py"),
        pathlib.Path("README.md"),
        pathlib.Path("style.css"),
    ]
    assert pyfltr.command.targets.filter_by_globs(files, ["*.py"]) == [
        pathlib.Path("main.py"),
        pathlib.Path("test_main.py"),
    ]
    assert pyfltr.command.targets.filter_by_globs(files, ["*.md", "*.css"]) == [
        pathlib.Path("README.md"),
        pathlib.Path("style.css"),
    ]
    assert pyfltr.command.targets.filter_by_globs(files, ["*.rs"]) == []


def test_pick_targets_none_when_targets_is_none() -> None:
    result = pyfltr.command.targets.pick_targets(None, "ruff-check")
    assert result is None


def test_pick_targets_returns_entry_for_matching_command(tmp_path: pathlib.Path) -> None:
    file_a = tmp_path / "a.py"
    targets = {"ruff-check": pyfltr.command.only_failed.ToolTargets.with_files([file_a])}
    result = pyfltr.command.targets.pick_targets(targets, "ruff-check")
    assert result is not None
    assert result.mode == "files"
    assert result.files == (file_a,)


def test_pick_targets_returns_none_for_missing_command() -> None:
    targets: dict[str, pyfltr.command.only_failed.ToolTargets] = {}
    result = pyfltr.command.targets.pick_targets(targets, "mypy")
    assert result is None
