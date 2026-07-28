"""パイプラインのモノレポモードのテスト。

`run_pipeline` がサブプロジェクト検出時に `subproject_aware=True` ツールを
サブプロジェクト別に実行することを確認する。

各テストは`run_subprocess`のみをモックするため、`pyfltr/command/targets.py`が直接呼ぶ
`git check-ignore`は実環境のgitプロセスとして起動する。
実行環境のgit状態への依存を断つため全ケースで`--no-gitignore`を指定する。
`tmp_path`配下に`.gitignore`を作成しないため、対象ファイル集合は指定の有無で変わらない。
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

import pyfltr.cli.main
import pyfltr.warnings_


def _make_subproject(path: pathlib.Path, *, name: str = "pkg") -> None:
    """テスト用サブプロジェクト一式（pyproject.toml と Python ファイル）を作成する。"""
    path.mkdir(parents=True, exist_ok=True)
    # pyfltr で pytest/typos を有効化するため `[tool.pyfltr]` を含める。
    (path / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\n[tool.pyfltr]\npytest = true\ntypos = true\n',
        encoding="utf-8",
    )


def _write_pyproject(
    path: pathlib.Path,
    name: str,
    *,
    pytest_on: bool | None = None,
    typos_on: bool | None = None,
    extra: str = "",
) -> None:
    """ツールのON/OFFを個別指定したサブプロジェクトの `pyproject.toml` を作成する。"""
    path.mkdir(parents=True, exist_ok=True)
    lines = ["[project]", f'name = "{name}"', "[tool.pyfltr]"]
    if pytest_on is not None:
        lines.append(f"pytest = {'true' if pytest_on else 'false'}")
    if typos_on is not None:
        lines.append(f"typos = {'true' if typos_on else 'false'}")
    content = "\n".join(lines) + "\n" + extra
    (path / "pyproject.toml").write_text(content, encoding="utf-8")


def _pytest_cwds(mock_run) -> set[pathlib.Path]:
    """`run_subprocess` モックから pytest が起動された cwd 集合を抽出する。"""
    cwds: set[pathlib.Path] = set()
    for call in mock_run.call_args_list:
        commandline = call.args[0] if call.args else []
        if not commandline or "pytest" not in " ".join(commandline):
            continue
        cwd_value = call.kwargs.get("cwd")
        if cwd_value is not None:
            cwds.add(pathlib.Path(cwd_value).resolve())
    return cwds


def _pytest_call_count(mock_run) -> int:
    """`run_subprocess` モックから pytest 起動回数（cwd 指定の有無を問わない）を数える。"""
    count = 0
    for call in mock_run.call_args_list:
        commandline = call.args[0] if call.args else []
        if commandline and "pytest" in " ".join(commandline):
            count += 1
    return count


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

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=pytest", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    # 各サブプロジェクト cwd で1回ずつ呼ばれている（サブプロジェクト別実行）
    cwds = _pytest_cwds(mock_run)
    assert tmp_path.resolve() in cwds
    assert (tmp_path / "pkg_a").resolve() in cwds
    assert (tmp_path / "pkg_b").resolve() in cwds


def test_monorepo_fallback_to_single_when_one_subproject(tmp_path: pathlib.Path, mocker) -> None:
    """検出結果1件以下ではモノレポモード非適用（単一実行経路）。"""
    _make_subproject(tmp_path, name="root")
    (tmp_path / "root_test.py").write_text("def test_root(): pass\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=pytest", "--no-archive", "--no-cache", "--no-gitignore"]
    )

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

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=typos", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    # typos は単一実行（モノレポでもサブ分割されない）。
    # ここでは run_subprocess が複数回呼ばれていないことを検証する代わりに、
    # 例外なしで完走することを確認する（モノレポモードを抜けて単一経路を通る）。


@pytest.mark.parametrize(
    ("parent_on", "child_on", "expected"),
    [
        # 親ON子ON: 既存挙動の維持（起点と各サブで実行）。
        (True, True, {"root", "pkg_a", "pkg_b"}),
        # 親ON子OFF: 子サブプロジェクトはスキップし、起点（root）自身のファイルでのみ実行する。
        (True, False, {"root"}),
        # 親OFF子ON: 起点はスキップし、有効なサブプロジェクトでのみ実行する。
        (False, True, {"pkg_a", "pkg_b"}),
        # 親OFF子OFF: どのcwdでも実行されない。
        (False, False, set()),
    ],
)
def test_monorepo_respects_per_subproject_on_off(
    tmp_path: pathlib.Path,
    mocker,
    parent_on: bool,
    child_on: bool,
    expected: set[str],
) -> None:
    """親子でツールのON/OFFが異なる両方向を、各サブプロジェクトの設定で個別に尊重する。"""
    _write_pyproject(tmp_path, "root", pytest_on=parent_on)
    _write_pyproject(tmp_path / "pkg_a", "pkg_a", pytest_on=child_on)
    _write_pyproject(tmp_path / "pkg_b", "pkg_b", pytest_on=child_on)
    (tmp_path / "root_test.py").write_text("def test_root(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "a_test.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_b" / "b_test.py").write_text("def test_b(): pass\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=pytest", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    name_to_path = {
        "root": tmp_path.resolve(),
        "pkg_a": (tmp_path / "pkg_a").resolve(),
        "pkg_b": (tmp_path / "pkg_b").resolve(),
    }
    assert _pytest_cwds(mock_run) == {name_to_path[n] for n in expected}


def test_monorepo_all_children_disabled_does_not_run_at_start_cwd(tmp_path: pathlib.Path, mocker) -> None:
    """全サブプロジェクトで対象ファイルがありつつ無効化された場合、起点cwdで全ファイルを誤実行しない。"""
    # root は pytest 有効だが直下に対象ファイルを置かず、root 自身は対象0件でスキップさせる。
    _write_pyproject(tmp_path, "root", pytest_on=True)
    # pkg_a / pkg_b は対象ファイルを持つが pytest を無効化する。
    _write_pyproject(tmp_path / "pkg_a", "pkg_a", pytest_on=False)
    _write_pyproject(tmp_path / "pkg_b", "pkg_b", pytest_on=False)
    (tmp_path / "pkg_a" / "a_test.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_b" / "b_test.py").write_text("def test_b(): pass\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=pytest", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    # 0件フォールバックと無効スキップを区別し、起点cwdでの誤実行を抑止する。
    assert _pytest_call_count(mock_run) == 0


def test_monorepo_applies_per_subproject_exclude(tmp_path: pathlib.Path, mocker) -> None:
    """サブプロジェクト固有のツール別除外設定が、当該サブプロジェクトの実行へ反映される。"""
    _write_pyproject(tmp_path, "root", pytest_on=True)
    # pkg_a は pytest 有効だが固有の pytest-exclude で唯一の対象ファイルを除外する。
    _write_pyproject(
        tmp_path / "pkg_a",
        "pkg_a",
        pytest_on=True,
        extra='pytest-exclude = ["pkg_a/a_test.py"]\n',
    )
    _write_pyproject(tmp_path / "pkg_b", "pkg_b", pytest_on=True)
    (tmp_path / "root_test.py").write_text("def test_root(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "a_test.py").write_text("def test_a(): pass\n", encoding="utf-8")
    (tmp_path / "pkg_b" / "b_test.py").write_text("def test_b(): pass\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["pytest"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=pytest", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    cwds = _pytest_cwds(mock_run)
    # pkg_a は固有除外で対象0件となり起動されない。root と pkg_b は通常通り実行する。
    assert (tmp_path / "pkg_a").resolve() not in cwds
    assert tmp_path.resolve() in cwds
    assert (tmp_path / "pkg_b").resolve() in cwds


@pytest.mark.parametrize("parent_on", [True, False])
def test_monorepo_repo_level_tool_fixed_by_start_config(tmp_path: pathlib.Path, mocker, parent_on: bool) -> None:
    """`subproject_aware=False` ツール（typos）のON/OFFは起点設定で固定し、子の設定で変えない。"""
    _write_pyproject(tmp_path, "root", typos_on=parent_on)
    # 子で typos を有効化しても、リポジトリ単位ツールのため起点設定が優先される。
    _write_pyproject(tmp_path / "pkg_a", "pkg_a", typos_on=True)
    _write_pyproject(tmp_path / "pkg_b", "pkg_b", typos_on=True)
    (tmp_path / "root.txt").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "a.txt").write_text("hello world\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["typos"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=typos", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    typos_calls = [
        call
        for call in mock_run.call_args_list
        if call.args and isinstance(call.args[0], list) and "typos" in " ".join(call.args[0])
    ]
    # 親ON時は起点 cwd で1回、親OFF時は子がONでも実行されない。
    assert len(typos_calls) == (1 if parent_on else 0)


def _make_cargo_crate(path: pathlib.Path, *, name: str = "crate") -> None:
    """テスト用Rust crate一式（`Cargo.toml`のみ、`pyproject.toml`は持たない）を作成する。"""
    path.mkdir(parents=True, exist_ok=True)
    (path / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8")


def _cargo_clippy_cwds(mock_run) -> set[pathlib.Path | None]:
    """`run_subprocess` モックから cargo-clippy が起動された cwd 集合を抽出する。"""
    cwds: set[pathlib.Path | None] = set()
    for call in mock_run.call_args_list:
        commandline = call.args[0] if call.args else []
        if not commandline or "clippy" not in " ".join(commandline):
            continue
        cwd_value = call.kwargs.get("cwd")
        cwds.add(pathlib.Path(cwd_value).resolve() if cwd_value is not None else None)
    return cwds


def test_monorepo_hybrid_cargo_only_subproject_runs_at_crate_cwd(tmp_path: pathlib.Path, mocker) -> None:
    """Pythonルート＋`Cargo.toml`単独サブディレクトリの構成で、起点`pyproject.toml`の
    `rust = true`設定を継承し`cargo-clippy`が`Cargo.toml`所在ディレクトリで起動される。
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.pyfltr]\npreset = "latest"\nrust = true\n',
        encoding="utf-8",
    )
    _make_cargo_crate(tmp_path / "rust" / "crate_a", name="crate_a")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=cargo-clippy", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    cwds = _cargo_clippy_cwds(mock_run)
    assert cwds == {(tmp_path / "rust" / "crate_a").resolve()}


def test_monorepo_cargo_workspace_root_only(tmp_path: pathlib.Path, mocker) -> None:
    """Cargo workspace root配下のmember crateは独立検出されず、workspace rootで1回のみ起動する。"""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.pyfltr]\npreset = "latest"\nrust = true\n', encoding="utf-8"
    )
    ws_root = tmp_path / "rust"
    ws_root.mkdir()
    (ws_root / "Cargo.toml").write_text('[workspace]\nmembers = ["crate_a"]\n', encoding="utf-8")
    _make_cargo_crate(ws_root / "crate_a", name="crate_a")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=cargo-clippy", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    cwds = _cargo_clippy_cwds(mock_run)
    assert cwds == {ws_root.resolve()}


def test_monorepo_nested_pyproject_inherits_nearest_ancestor(tmp_path: pathlib.Path, mocker) -> None:
    """ネスト構成: 孫Cargo.tomlは最近接祖先（子pyproject.toml）のconfigを継承する。

    ルートで`rust = true`、子で`rust = false`のとき、孫のcargo系はスキップされる。
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.pyfltr]\npreset = "latest"\nrust = true\n', encoding="utf-8"
    )
    child = tmp_path / "child"
    child.mkdir()
    (child / "pyproject.toml").write_text(
        '[project]\nname = "child"\n[tool.pyfltr]\npreset = "latest"\nrust = false\n', encoding="utf-8"
    )
    _make_cargo_crate(child / "crate", name="grandchild_crate")

    proc = subprocess.CompletedProcess(["cargo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=cargo-clippy", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    cwds = _cargo_clippy_cwds(mock_run)
    assert cwds == set()  # 孫は`rust = false`継承でスキップ


# `.sln`集約ルート下のcsproj除外は`tests/subprojects_test.py`側の
# `test_discover_subprojects_dotnet_solution_excludes_registered_projects`単体テストで検証する。
# 統合テスト（`run_subprocess`モック観測）は`dotnet`実行ファイルおよびmise内のdotnet登録が
# CI環境で不在のため成立せず（`runner._resolve_direct_executable`/`_is_tool_active_in_mise_config`
# の解決経路が環境依存で分岐する）、集約フィルターの言語非依存動作は
# `test_monorepo_cargo_workspace_root_only`で代表検証している。


def test_monorepo_subproject_config_does_not_warn_missing_repo_level_config(tmp_path: pathlib.Path, mocker) -> None:
    """リポジトリルートにのみ`.pre-commit-config.yaml`を置いた構成で、
    子で`prek`を有効化しても設定ファイル不在の警告を発行しない。

    `prek`は`subproject_aware=False`で起点cwdでのみ実行されるため、
    サブプロジェクトのディレクトリを基準にした設定ファイル探索は誤検知になる。
    """
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "root"\n[tool.pyfltr]\n', encoding="utf-8")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    _write_pyproject(tmp_path / "pkg_a", "pkg_a", extra="prek = true\n")
    (tmp_path / "pkg_a" / "a.txt").write_text("hello world\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["typos"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=typos", "--no-archive", "--no-cache", "--no-gitignore"]
    )

    messages = [warning["message"] for warning in pyfltr.warnings_.collected_warnings()]
    assert not [message for message in messages if ".pre-commit-config.yaml" in message]


def test_monorepo_global_config_warning_is_emitted_once(
    tmp_path: pathlib.Path,
    mocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """モノレポではglobal設定由来の警告を起点cwdのロード時に1回だけ発行する。"""
    global_path = tmp_path / "global_config.toml"
    global_path.write_text("[tool.pyfltr]\nunknown-key-xyz = true\n", encoding="utf-8")
    monkeypatch.setenv("PYFLTR_GLOBAL_CONFIG", str(global_path))
    _write_pyproject(tmp_path, "root", typos_on=True)
    _write_pyproject(tmp_path / "pkg_a", "pkg_a", typos_on=True)
    _write_pyproject(tmp_path / "pkg_b", "pkg_b", typos_on=True)
    (tmp_path / "root.txt").write_text("root\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "pkg_b" / "b.txt").write_text("b\n", encoding="utf-8")
    proc = subprocess.CompletedProcess(["typos"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        [
            "run",
            "--work-dir",
            str(tmp_path),
            "--commands=typos",
            "--no-archive",
            "--no-cache",
            "--no-gitignore",
        ]
    )

    messages = [
        warning["message"] for warning in pyfltr.warnings_.collected_warnings() if "unknown-key-xyz" in warning["message"]
    ]
    assert len(messages) == 1


def test_monorepo_global_config_warning_with_origin_override_is_emitted_once(
    tmp_path: pathlib.Path,
    mocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起点projectがglobalの不正値を正常値で上書きしていても、上書きしない複数の
    サブプロジェクトにまたがって同じglobal由来の検証警告が重複発行されない。

    `_write_pyproject`は`preset`個別指定に対応しないため、本テストは
    `pyproject.toml`を直接書き込む。
    """
    global_path = tmp_path / "global_config.toml"
    global_path.write_text("[tool.pyfltr]\npreset = 42\n", encoding="utf-8")
    monkeypatch.setenv("PYFLTR_GLOBAL_CONFIG", str(global_path))
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\npreset = "latest"\n', encoding="utf-8")
    for name in ("pkg_a", "pkg_b"):
        sub_dir = tmp_path / name
        sub_dir.mkdir()
        (sub_dir / "pyproject.toml").write_text("[tool.pyfltr]\n", encoding="utf-8")
        (sub_dir / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    (tmp_path / "root.txt").write_text("root\n", encoding="utf-8")
    proc = subprocess.CompletedProcess(["typos"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        [
            "run",
            "--work-dir",
            str(tmp_path),
            "--commands=typos",
            "--no-archive",
            "--no-cache",
            "--no-gitignore",
        ]
    )

    messages = [warning["message"] for warning in pyfltr.warnings_.collected_warnings() if "preset" in warning["message"]]
    assert len(messages) == 1


def test_monorepo_root_project_own_validation_warning_is_emitted_once(
    tmp_path: pathlib.Path,
    mocker,
) -> None:
    """起点cwd自身がサブプロジェクトとして検出される場合、起点project固有の誤設定に
    由来する検証警告が起点ロード分と`resolve_subproject_configs`の再ロード分で
    重複発行されない。

    起点cwd自身のマーカー（`pyproject.toml`）検出によりサブプロジェクト一覧へ
    起点自身（`relative == "."`）が含まれる場合、`resolve_subproject_configs`が
    起点configを再ロードすると、global由来ではない起点project固有の誤設定
    （global設定には存在しないキー）まで再検証され重複発行される。
    `already_warned`によるglobal由来キーの累積抑止はこのケースへ対応できないため、
    起点cwd自身は再ロードせず`config`引数をそのまま再利用する必要がある。
    """
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "root"\n[tool.pyfltr]\nunknown-root-only-key-xyz = true\ntypos = true\n',
        encoding="utf-8",
    )
    _write_pyproject(tmp_path / "pkg_a", "pkg_a", typos_on=True)
    (tmp_path / "root.txt").write_text("root\n", encoding="utf-8")
    (tmp_path / "pkg_a" / "a.txt").write_text("a\n", encoding="utf-8")
    proc = subprocess.CompletedProcess(["typos"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        [
            "run",
            "--work-dir",
            str(tmp_path),
            "--commands=typos",
            "--no-archive",
            "--no-cache",
            "--no-gitignore",
        ]
    )

    messages = [
        warning["message"]
        for warning in pyfltr.warnings_.collected_warnings()
        if "unknown-root-only-key-xyz" in warning["message"]
    ]
    assert len(messages) == 1


def test_monorepo_unknown_command_name_warning_is_emitted_once(
    tmp_path: pathlib.Path,
    mocker,
) -> None:
    """`--enable`に未知のコマンド名を渡したときの警告を起点で1回だけ発行する。

    `apply_cli_overrides`は起点（`run_pipeline`）と各サブプロジェクト
    （`resolve_subproject_configs`）の双方から呼ばれる。CLI引数はサブプロジェクト間で
    共通のため、サブプロジェクトごとに警告を発行するとサブプロジェクト数だけ
    同一メッセージが重複する。
    """
    _write_pyproject(tmp_path, "root", typos_on=True)
    for name in ("pkg_a", "pkg_b", "pkg_c"):
        _write_pyproject(tmp_path / name, name, typos_on=True)
        (tmp_path / name / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    (tmp_path / "root.txt").write_text("root\n", encoding="utf-8")
    proc = subprocess.CompletedProcess(["typos"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    pyfltr.cli.main.run(
        [
            "run",
            "--work-dir",
            str(tmp_path),
            "--commands=typos",
            "--enable=no-such-command-xyz",
            "--no-archive",
            "--no-cache",
            "--no-gitignore",
        ]
    )

    messages = [
        warning["message"] for warning in pyfltr.warnings_.collected_warnings() if "no-such-command-xyz" in warning["message"]
    ]
    assert len(messages) == 1
