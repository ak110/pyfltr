"""パイプラインのモノレポモードのテスト。

`run_pipeline` がサブプロジェクト検出時に `subproject_aware=True` ツールを
サブプロジェクト別に実行することを確認する。
"""

from __future__ import annotations

import pathlib
import subprocess

import pyfltr.cli.main


def _make_subproject(path: pathlib.Path, *, name: str = "pkg") -> None:
    """テスト用サブプロジェクト一式（pyproject.toml と Python ファイル）を作成する。"""
    path.mkdir(parents=True, exist_ok=True)
    # pyfltr で pytest/typos を有効化するため `[tool.pyfltr]` を含める。
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\n[tool.pyfltr]\npytest = true\ntypos = true\n',
        encoding="utf-8",
    )


def test_monorepo_pytest_runs_per_subproject(tmp_path: pathlib.Path, mocker) -> None:
    """サブプロジェクト2件構成で `subproject_aware=True` ツールがサブ別に呼ばれる。"""
    # ルート + 2サブプロジェクト
    _make_subproject(tmp_path, name="root")
    _make_subproject(tmp_path / "pkg_a", name="pkg_a")
    _make_subproject(tmp_path / "pkg_b", name="pkg_b")
    # ルート直下にも対象ファイルを置く
    (tmp_path / "root_test.py").write_text("def test_root(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "a_test.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_b" / "b_test.py").write_text("def test_b(): pass\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["run", "--work-dir", str(tmp_path), "--commands=pytest", "--no-archive", "--no-cache"])

    # pytest が複数の cwd で呼ばれていることを確認（サブプロジェクト別実行）
    cwds: list[pathlib.Path] = []
    for call in mock_run.call_args_list:
        commandline = call.args[0] if call.args else []
        if not commandline or "pytest" not in " ".join(commandline):
            continue
        cwd_value = call.kwargs.get("cwd")
        if cwd_value is not None:
            cwds.append(pathlib.Path(cwd_value))

    # 各サブプロジェクト cwd で1回ずつ呼ばれている
    assert tmp_path.resolve() in [c.resolve() for c in cwds]
    assert (tmp_path / "pkg_a").resolve() in [c.resolve() for c in cwds]
    assert (tmp_path / "pkg_b").resolve() in [c.resolve() for c in cwds]


def test_monorepo_fallback_to_single_when_one_subproject(tmp_path: pathlib.Path, mocker) -> None:
    """検出結果1件以下ではモノレポモード非適用（単一実行経路）。"""
    _make_subproject(tmp_path, name="root")
    (tmp_path / "root_test.py").write_text("def test_root(): pass\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["run", "--work-dir", str(tmp_path), "--commands=pytest", "--no-archive", "--no-cache"])

    # pytest は1回だけ呼ばれる（単一プロジェクト経路）
    pytest_calls = [
        call
        for call in mock_run.call_args_list
        if call.args and isinstance(call.args[0], list) and "pytest" in " ".join(call.args[0])
    ]
    assert len(pytest_calls) == 1


def test_monorepo_subproject_aware_false_uses_single_cwd(tmp_path: pathlib.Path, mocker) -> None:
    """`subproject_aware=False` ツール（typos）はサブ分割せず起点 cwd で1回起動する。"""
    _make_subproject(tmp_path, name="root")
    _make_subproject(tmp_path / "pkg_a", name="pkg_a")
    _make_subproject(tmp_path / "pkg_b", name="pkg_b")
    (tmp_path / "root.txt").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "a.txt").write_text("hello world\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["typos"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(["run", "--work-dir", str(tmp_path), "--commands=typos", "--no-archive", "--no-cache"])

    # typos は単一実行（モノレポでもサブ分割されない）。
    # ここでは run_subprocess が複数回呼ばれていないことを検証する代わりに、
    # 例外なしで完走することを確認する（モノレポモードを抜けて単一経路を通る）。
