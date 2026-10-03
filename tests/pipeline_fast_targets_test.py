"""`pytest-fast-targets`によるfast選択時のpytest対象のテスト。

fast選択（`fast`サブコマンドまたは`--commands`のfastエイリアス）でだけ、pytestの対象が
位置引数・差分指定に依らずプロジェクト全域の`pytest-fast-targets`一致ファイルになることを確かめる。
`run_subprocess`だけをモックし、pytestへ渡ったファイル引数で対象を判定する。
gitの状態に依存しないよう、`--changed-since`を使うケース以外は`--no-gitignore`を指定する。
"""

from __future__ import annotations

import pathlib
import subprocess

import pyfltr.cli.main


def _write_project(path: pathlib.Path, *, extra: str) -> None:
    """不変条件テスト1件、importで失敗する通常テスト1件、Markdown1件を持つプロジェクトを作成する。"""
    (path / "pyproject.toml").write_text(f'[project]\nname = "proj"\n[tool.pyfltr]\n{extra}', encoding="utf-8")
    (path / "tests").mkdir()
    (path / "tests" / "repo_invariant_test.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    (path / "tests" / "other_test.py").write_text("import no_such_module\n", encoding="utf-8")
    (path / "README.md").write_text("# doc\n", encoding="utf-8")


def _pytest_file_args(mock_run) -> list[list[str]]:
    """pytest起動ごとに、コマンドライン末尾の`.py`引数を返す。"""
    result: list[list[str]] = []
    for call in mock_run.call_args_list:
        commandline = call.args[0] if call.args else []
        if commandline and "pytest" in " ".join(commandline):
            result.append([pathlib.Path(arg).as_posix() for arg in commandline if str(arg).endswith(".py")])
    return result


def _run(tmp_path: pathlib.Path, *argv: str) -> int:
    return pyfltr.cli.main.run([*argv, "--work-dir", str(tmp_path), "--no-archive", "--no-cache"])


FAST_CONFIG = 'pytest = true\npytest-fast-targets = ["*_invariant_test.py"]\n'


def test_fast_runs_fast_targets_when_only_markdown_is_given(tmp_path: pathlib.Path, mocker) -> None:
    """Markdownだけを渡したfastでも、指定globに一致するテストだけをpytestへ渡す。"""
    _write_project(tmp_path, extra=FAST_CONFIG)
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    rc = _run(tmp_path, "fast", "--no-gitignore", str(tmp_path / "README.md"))

    assert rc == 0
    assert _pytest_file_args(mock_run) == [["tests/repo_invariant_test.py"]]


def test_fast_with_explicit_pytest_command_uses_fast_targets(tmp_path: pathlib.Path, mocker) -> None:
    """`fast --commands=pytest`もfast選択として指定globを使う。"""
    _write_project(tmp_path, extra=FAST_CONFIG)
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "fast", "--commands=pytest", "--no-gitignore")

    assert _pytest_file_args(mock_run) == [["tests/repo_invariant_test.py"]]


def test_run_commands_fast_with_changed_since_reports_failure(tmp_path: pathlib.Path, mocker) -> None:
    """差分が別ファイルだけでも指定テストを実行し、失敗を非0終了で返す。"""
    _write_project(tmp_path, extra=FAST_CONFIG)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "README.md").write_text("# changed\n", encoding="utf-8")

    def _fake(commandline, *_args, **_kwargs):
        # pytestだけを失敗させる（他のコマンドは有効化していない）。
        return subprocess.CompletedProcess(commandline, returncode=1, stdout="FAILED tests/repo_invariant_test.py\n")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_fake)

    rc = _run(tmp_path, "run", "--commands=fast", "--changed-since=HEAD")

    assert rc == 1
    assert _pytest_file_args(mock_run) == [["tests/repo_invariant_test.py"]]


def test_fast_with_no_matching_file_does_not_start_pytest(tmp_path: pathlib.Path, mocker) -> None:
    """一致0件ではpytestを起動せず、全件収集へ戻らない。"""
    _write_project(tmp_path, extra='pytest = true\npytest-fast-targets = "*_nothing_test.py"\n')
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    rc = _run(tmp_path, "fast", "--commands=pytest", "--no-gitignore")

    assert rc == 0
    assert not _pytest_file_args(mock_run)


def test_run_and_ci_keep_normal_pytest_targets(tmp_path: pathlib.Path, mocker) -> None:
    """fastトークンの無いrun・ciでは、位置引数と`pytest-targets`で対象を決める。"""
    _write_project(tmp_path, extra=FAST_CONFIG)
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    _run(tmp_path, "run", "--commands=pytest", "--no-gitignore", str(tmp_path / "tests" / "other_test.py"))
    _run(tmp_path, "ci", "--commands=pytest", "--no-gitignore")

    runs = _pytest_file_args(mock_run)
    assert len(runs) == 2
    assert [pathlib.PurePath(arg).name for arg in runs[0]] == ["other_test.py"]
    assert sorted(runs[1]) == ["tests/other_test.py", "tests/repo_invariant_test.py"]


def test_fast_without_setting_or_with_pytest_disabled_does_not_run_pytest(tmp_path: pathlib.Path, mocker) -> None:
    """空設定ではpytestはfastへ参加せず、`pytest = false`では指定があっても実行しない。"""
    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    empty = tmp_path / "empty"
    empty.mkdir()
    _write_project(empty, extra="pytest = true\npytest-fast-targets = []\n")
    disabled = tmp_path / "disabled"
    disabled.mkdir()
    _write_project(disabled, extra='pytest = false\npytest-fast-targets = ["*_invariant_test.py"]\n')

    _run(empty, "fast", "--no-gitignore")
    _run(disabled, "fast", "--no-gitignore")

    assert not _pytest_file_args(mock_run)
