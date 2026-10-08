from __future__ import annotations

import pathlib
import subprocess

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_server
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run

ALWAYS_CONFIG = 'pytest = true\npytest-always-targets = ["*_invariant_test.py"]\n'


def _write_project(path: pathlib.Path, *, extra: str) -> None:
    """通常テスト2件、横断テスト1件、Markdown1件を持つプロジェクトを作成する。"""
    (path / "pyproject.toml").write_text(f'[project]\nname = "proj"\n[tool.pyfltr]\n{extra}', encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "repo_invariant_test.py").write_text("def test_invariant(): pass\n", encoding="utf-8")
    (path / "tests" / "selected_test.py").write_text("def test_selected(): pass\n", encoding="utf-8")
    (path / "tests" / "other_test.py").write_text("def test_other(): pass\n", encoding="utf-8")
    (path / "README.md").write_text("# doc\n", encoding="utf-8")


def _pytest_file_args(mock_run) -> list[list[str]]:
    """pytest起動ごとに、コマンドライン末尾の`.py`引数をファイル名で返す。"""
    result: list[list[str]] = []
    for call in mock_run.call_args_list:
        commandline = call.args[0] if call.args else []
        if commandline and "pytest" in " ".join(commandline):
            result.append(sorted(pathlib.PurePath(arg).name for arg in commandline if str(arg).endswith(".py")))
    return result


def _fail_invariant(commandline, *_args, **_kwargs):
    """横断テストを含むpytest起動だけを失敗させる。"""
    failed = any(str(arg).endswith("repo_invariant_test.py") for arg in commandline)
    output = "FAILED tests/repo_invariant_test.py::test_invariant\n" if failed else ""
    return subprocess.CompletedProcess(commandline, returncode=1 if failed else 0, stdout=output)


def _run(tmp_path: pathlib.Path, *argv: str) -> int:
    return pyfltr.cli.main.run([*argv, "--work-dir", str(tmp_path), "--no-archive", "--no-cache", "--no-gitignore"])


@pytest.mark.parametrize("subcommand", ["run", "run-for-agent", "ci"])
def test_run_adds_always_targets_and_reports_failure(tmp_path: pathlib.Path, mocker, subcommand: str) -> None:
    """通常対象だけを渡しても横断テストがpytestへ渡り、その失敗が非0終了になる。"""
    _write_project(tmp_path, extra=ALWAYS_CONFIG)
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_fail_invariant)

    rc = _run(tmp_path, subcommand, "--commands=pytest", str(tmp_path / "tests" / "selected_test.py"))

    assert rc == 1
    assert _pytest_file_args(mock_run) == [["repo_invariant_test.py", "selected_test.py"]]


@pytest.mark.asyncio
async def test_mcp_run_adds_always_targets_and_reports_failure(tmp_path: pathlib.Path, mocker) -> None:
    """MCPの`run`でも横断テストが加わり、失敗が結果へ現れる。"""
    _write_project(tmp_path, extra=ALWAYS_CONFIG + "respect-gitignore = false\n")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_fail_invariant)

    result = await pyfltr.cli.mcp_server.tool_run(paths=["tests/selected_test.py"], commands=["pytest"], work_dir=str(tmp_path))

    assert result.exit_code == 1
    assert "pytest" in result.failed
    assert _pytest_file_args(mock_run) == [["repo_invariant_test.py", "selected_test.py"]]


def test_non_python_only_and_changed_since_reach_always_targets(tmp_path: pathlib.Path, mocker) -> None:
    """非Pythonファイルだけの入力と、差分が別ファイルだけの`--changed-since`でも横断テストへ到達する。"""
    _write_project(tmp_path, extra=ALWAYS_CONFIG)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "README.md").write_text("# changed\n", encoding="utf-8")
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "run", "--commands=pytest", str(tmp_path / "README.md"))
    _run(tmp_path, "run", "--commands=pytest", "--changed-since=HEAD")

    assert _pytest_file_args(mock_run) == [["repo_invariant_test.py"], ["repo_invariant_test.py"]]


def test_explicit_always_target_is_passed_once(tmp_path: pathlib.Path, mocker) -> None:
    """横断テスト自体を位置引数で渡しても1回だけpytestへ渡す。"""
    _write_project(tmp_path, extra=ALWAYS_CONFIG)
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "run", "--commands=pytest", str(tmp_path / "tests" / "repo_invariant_test.py"))

    assert _pytest_file_args(mock_run) == [["repo_invariant_test.py"]]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param("pytest = true\n", [["selected_test.py"]], id="unset"),
        pytest.param(ALWAYS_CONFIG + 'pytest-exclude = ["*_invariant_test.py"]\n', [["selected_test.py"]], id="excluded"),
        pytest.param('pytest = false\npytest-always-targets = ["*_invariant_test.py"]\n', [], id="pytest-disabled"),
    ],
)
def test_boundaries_keep_existing_selection(tmp_path: pathlib.Path, mocker, extra: str, expected: list[list[str]]) -> None:
    """未設定・除外・pytest無効では横断テストを加えない。"""
    _write_project(tmp_path, extra=extra)
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "run", str(tmp_path / "tests" / "selected_test.py"))

    assert _pytest_file_args(mock_run) == expected


def test_fast_merges_fast_targets_and_always_targets(tmp_path: pathlib.Path, mocker) -> None:
    """fastでは`pytest-fast-targets`の置換対象と横断テストの和集合を実行する。"""
    _write_project(
        tmp_path,
        extra='pytest = true\npytest-fast-targets = ["selected_test.py"]\npytest-always-targets = ["*_invariant_test.py"]\n',
    )
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "fast", str(tmp_path / "README.md"))

    assert _pytest_file_args(mock_run) == [["repo_invariant_test.py", "selected_test.py"]]


def test_fast_does_not_add_pytest_by_always_targets_only(tmp_path: pathlib.Path, mocker) -> None:
    """fastへpytestが参加しない設定では、`pytest-always-targets`だけでpytestを加えない。"""
    _write_project(tmp_path, extra=ALWAYS_CONFIG)
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "fast", str(tmp_path / "README.md"))

    assert not _pytest_file_args(mock_run)


def test_only_failed_limits_candidates_to_failed_always_target(
    tmp_path: pathlib.Path, mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--only-failed`では横断テストを含む候補から前回失敗したファイルだけを実行する。"""
    project = tmp_path / "proj"
    project.mkdir()
    _write_project(project, extra=ALWAYS_CONFIG)
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(cache_root))
    _seed_run(
        cache_root,
        commands=["pytest"],
        exit_code=1,
        tool_results=[("pytest", 1, "", [_make_error("pytest", "tests/repo_invariant_test.py", 1, "failed")])],
    )
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        [
            "run",
            "--commands=pytest",
            "--only-failed",
            "--work-dir",
            str(project),
            "--no-cache",
            "--no-gitignore",
            str(project / "tests" / "selected_test.py"),
        ]
    )

    assert _pytest_file_args(mock_run) == [["repo_invariant_test.py"]]


def test_monorepo_reaches_each_subproject_always_targets(tmp_path: pathlib.Path, mocker) -> None:
    """起点と各サブプロジェクトの設定をそれぞれのcwdで解釈し、変更の無いサブプロジェクトの横断テストへも到達する。"""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "root"\n[tool.pyfltr]\n', encoding="utf-8")
    for name, globs in [("pkg_a", '["*_invariant_test.py"]'), ("pkg_b", '"check_*.py"'), ("pkg_c", None)]:
        sub = tmp_path / name
        sub.mkdir()
        extra = f"pytest-always-targets = {globs}\n" if globs else ""
        (sub / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\n[tool.pyfltr]\npytest = true\n{extra}', encoding="utf-8"
        )
    (tmp_path / "pkg_a" / "repo_invariant_test.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "other_test.py").write_text("def test_o(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_b" / "check_layout.py").write_text("def test_b(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_b" / "b_test.py").write_text("def test_b2(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_c" / "c_test.py").write_text("def test_c(): pass\n", encoding="utf-8")
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "run", "--commands=pytest", str(tmp_path / "pkg_b" / "b_test.py"))

    files_by_cwd: dict[pathlib.Path, list[str]] = {}
    for call in mock_run.call_args_list:
        commandline = call.args[0] if call.args else []
        if commandline and "pytest" in " ".join(commandline):
            cwd = pathlib.Path(call.kwargs["cwd"]).resolve()
            files_by_cwd[cwd] = sorted(pathlib.PurePath(arg).name for arg in commandline if str(arg).endswith(".py"))
    assert files_by_cwd == {
        (tmp_path / "pkg_a").resolve(): ["repo_invariant_test.py"],
        (tmp_path / "pkg_b").resolve(): ["b_test.py", "check_layout.py"],
    }
