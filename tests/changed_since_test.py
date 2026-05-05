"""--changed-since オプションのテスト。

git 差分ファイルへのフィルタリング・空集合・ref 不在フォールバック・
git 不在フォールバックを検証する。
"""

# pylint: disable=missing-function-docstring  # テストは関数docstringを省略する慣習

import collections.abc
import os
import pathlib
import subprocess

import pytest

import pyfltr.command.targets
import pyfltr.warnings_

# ---------------------------------------------------------------------------
# filter_by_changed_since / _get_changed_files の単体テスト
# ---------------------------------------------------------------------------


@pytest.fixture(name="_git_repo")
def _git_repo_fixture(tmp_path: pathlib.Path) -> collections.abc.Generator[pathlib.Path]:
    """一時 git リポジトリを作成して cwd を切り替えるフィクスチャ。

    テスト終了時に元の cwd へ復元する。
    """
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    subprocess.run(["git", "init"], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], check=True, capture_output=True)
    yield tmp_path
    os.chdir(original_cwd)


def test_filter_by_changed_since_normal(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    """通常ケース: HEAD からの変更ファイルだけが対象になる。"""
    # 初期コミット
    committed = tmp_path / "committed.py"
    committed.write_text("x = 1\n")
    unchanged = tmp_path / "unchanged.py"
    unchanged.write_text("y = 2\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    # HEAD 以降の変更（未コミット作業ツリー差分）
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
    # 警告は出ないこと
    assert not pyfltr.warnings_.collected_warnings()


def test_filter_by_changed_since_empty_diff(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    """空集合ケース: 差分がない場合は空リストを返す。"""
    initial = tmp_path / "initial.py"
    initial.write_text("x = 1\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    all_files = [pathlib.Path("initial.py")]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD")

    assert result == []
    assert not pyfltr.warnings_.collected_warnings()


def test_filter_by_changed_since_invalid_ref(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    """ref 不在ケース: 存在しない ref を指定した場合に警告を発行して全体実行へフォールバックする。"""
    initial = tmp_path / "a.py"
    initial.write_text("x = 1\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    all_files = [pathlib.Path("a.py")]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "no-such-branch-xyz")

    # フォールバック: 全体実行相当のリストを返す
    assert result == all_files
    warnings = pyfltr.warnings_.collected_warnings()
    assert len(warnings) == 1
    assert warnings[0]["source"] == "changed-since"
    assert "no-such-branch-xyz" in warnings[0]["message"]


def test_filter_by_changed_since_git_not_found(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """git 不在ケース: PATH 制限で git が見つからない場合にフォールバックする。"""
    # PATH を空にして git を解決不能にする
    monkeypatch.setenv("PATH", str(tmp_path))

    all_files = [pathlib.Path("a.py")]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD")

    assert result == all_files
    warnings = pyfltr.warnings_.collected_warnings()
    assert len(warnings) == 1
    assert warnings[0]["source"] == "changed-since"
    assert "git が見つからない" in warnings[0]["message"]


def test_filter_by_changed_since_excludes_untracked(tmp_path: pathlib.Path, _git_repo: pathlib.Path) -> None:
    """untracked ケース: git add 未実施の新規ファイルは対象から除外される。"""
    # 初期コミット
    tracked = tmp_path / "tracked.py"
    tracked.write_text("x = 1\n")
    subprocess.run(["git", "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "commit", "--message=initial"], check=True, capture_output=True)

    # tracked ファイルを変更（staged）
    tracked.write_text("x = 2\n")
    subprocess.run(["git", "add", str(tracked)], check=True, capture_output=True)

    # untracked ファイル（git add 未実施）
    untracked = tmp_path / "untracked.py"
    untracked.write_text("y = 3\n")

    all_files = [
        pathlib.Path("tracked.py"),
        pathlib.Path("untracked.py"),
    ]
    result = pyfltr.command.targets.filter_by_changed_since(all_files, "HEAD")

    # tracked の変更ファイルのみが対象になり、untracked は除外される
    assert result == [pathlib.Path("tracked.py")]
    assert not pyfltr.warnings_.collected_warnings()
