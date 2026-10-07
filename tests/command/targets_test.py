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


def test_excluded_default_patterns() -> None:
    """DEFAULT_CONFIG["exclude"]が主要パターンに対して正しく動作することを確認する。"""
    config = pyfltr.config.config.create_default_config()

    # 直接マッチ（ディレクトリ名）
    assert pyfltr.command.targets.excluded(pathlib.Path(".serena"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".cursor"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".idea"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".venv"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path("node_modules"), config)

    # 親ディレクトリマッチ（配下ファイル）
    assert pyfltr.command.targets.excluded(pathlib.Path(".serena/memories/foo.md"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".cursor/rules/bar.mdc"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".idea/workspace.xml"), config)

    # ワイルドカードパターン（.aider*）
    assert pyfltr.command.targets.excluded(pathlib.Path(".aider.conf.yml"), config)
    assert pyfltr.command.targets.excluded(pathlib.Path(".aider.chat.history.md"), config)

    # 無関係なパスは除外されないこと
    assert not pyfltr.command.targets.excluded(pathlib.Path("pyfltr/config.py"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path("tests/command_test.py"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path("README.md"), config)

    # 戻り値は（設定キー, 一致パターン）のタプル
    config.values["exclude"] = []
    config.values["extend-exclude"] = ["sample.py"]
    assert pyfltr.command.targets.excluded(pathlib.Path("sample.py"), config) == ("extend-exclude", "sample.py")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # ロックファイル（除外対象）
        pytest.param("pnpm-lock.yaml", True, id="pnpm-lock.yaml"),
        pytest.param("yarn.lock", True, id="yarn.lock"),
        pytest.param("uv.lock", True, id="uv.lock"),
        # minify済みファイル（除外対象）
        pytest.param("app.min.js", True, id="app.min.js"),
        pytest.param("style.min.css", True, id="style.min.css"),
        # source map（除外対象）
        pytest.param("app.js.map", True, id="app.js.map"),
        # cargo-denyトリガーとして保持（除外対象外）
        pytest.param("Cargo.lock", False, id="Cargo.lock"),
        # minify/source mapを含まない通常ファイル（除外対象外）
        pytest.param("config.yaml", False, id="config.yaml"),
        pytest.param("app.js", False, id="app.js"),
        pytest.param("map.txt", False, id="map.txt"),
    ],
)
def test_excluded_added_default_patterns(name: str, expected: bool) -> None:
    """追加されたDEFAULT_CONFIG["exclude"]パターン（ロックファイル・minify済み・source map）の動作を確認する。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.targets.excluded(pathlib.Path(name), config)
    if expected:
        assert result
    else:
        assert not result


def test_excluded_disabled_by_empty_config() -> None:
    """exclude/extend-excludeが空の場合、全パスが除外されないことを確認する（--no-exclude相当）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["exclude"] = []
    config.values["extend-exclude"] = []

    # 通常は除外されるパスが除外されないこと
    assert not pyfltr.command.targets.excluded(pathlib.Path(".venv"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path("node_modules"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path(".serena/memories/foo.md"), config)


def test_expand_all_files_reuses_shared_parent_exclude_match(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一走査内では共有する親ディレクトリの除外照合を1回だけ行う。"""
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
    """連続実行で変更した除外設定へ前回の照合結果を持ち越さない。"""
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


def test_expand_all_files_respects_gitignore(tmp_path: pathlib.Path) -> None:
    """.gitignoreに記載されたファイルがexpand_all_filesから除外される。"""
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
    """respect-gitignore = falseの場合、.gitignoreによるフィルタリングが無効になる。"""
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


def test_expand_all_files_no_git_repo(tmp_path: pathlib.Path) -> None:
    """gitリポジトリ外でも正常に動作する。"""
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
    """直接指定されたファイルがexclude設定で除外された場合に警告が出る。"""
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
        # stderrにも、除外を無視して検査する指定方法が対処として出る
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
    """ディレクトリ走査のI/Oエラー警告でも区切り文字を`/`へ統一する。"""
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
    # 例外の要約に続けて、利用者が確かめる対象（存在と権限）を案内する
    assert "読み取り権限を確認してください" in caplog.text


def test_expand_all_files_warns_missing_file(tmp_path: pathlib.Path, caplog) -> None:
    """直接指定されたパスが存在しない場合、警告が出て reason="missing" で蓄積される。"""
    # 絶対パス指定はcwd起点の相対パスへ変換されるため、cwd配下の相対パスとして検証する。
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
        # 非存在は reason="missing" に蓄積され、reason="excluded" とは別系統
        assert normalized_target in caplog.text
        assert pyfltr.warnings_.filtered_direct_files(reason="missing") == [normalized_target]
        assert not pyfltr.warnings_.filtered_direct_files(reason="excluded")
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_warns_gitignored_file(tmp_path: pathlib.Path, caplog) -> None:
    """直接指定されたファイルが.gitignoreで除外された場合に警告が出る。"""
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
    """シンボリックリンクディレクトリ越しに辿ったファイルでも.gitignore判定が成立する。

    過去にsymlink越えのpathspecで`git check-ignore`がreturncode 128を返すと
    .gitignore判定が丸ごとスキップされ、未追跡ファイルが対象に残る不具合があった。
    """
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
    """git rev-parseが正常終了した場合、標準出力に応じて作業ツリー状態を返す。"""
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
    """git rev-parseがタイムアウトした場合は判定不能を返す。"""

    def timeout_run(args: list[str], **kwargs: typing.Any) -> typing.NoReturn:
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr("pyfltr.command.targets.subprocess.run", timeout_run)

    assert pyfltr.command.targets._work_tree_state(tmp_path) == "unknown"  # pylint: disable=protected-access  # タイムアウト時の分類を直接検証する


def test_work_tree_state_returns_unknown_on_git_dir_probe_timeout(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GIT_DIRの解決確認がタイムアウトした場合は判定不能を返す。"""
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
    """git check-ignoreが想定外の終了コードを返した場合にwarningが出て部分結果が活用される。"""
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
        # stdoutで返ってきたignored.pyは部分結果として除外される
        assert "ignored.py" not in names
        collected = pyfltr.warnings_.collected_warnings()
        assert any("git check-ignore" in w["message"] and "128" in w["message"] for w in collected)
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_keeps_files_outside_git_worktree(tmp_path: pathlib.Path) -> None:
    """Git作業ツリー外でgit check-ignoreが終了コード128を返しても対象ファイルを保持する。"""
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
    """Git作業ツリー外でも128以外の異常終了では警告を発行し部分結果を除外する。"""
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
    """gitignore判定と作業ツリー判定を同じ失敗へ固定する。"""
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
    """指定cwdでPython対象名と警告を取得し、プロセス状態を復元する。"""
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
    """Git作業ツリーの内外を判定できない場合は警告を発行し部分結果を除外する。"""
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
    """Gitの環境指定がある場合、祖先外の判定不能な128を警告なしで外側としない。"""
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
    """GIT_WORK_TREEだけが指定された非Gitディレクトリでは128の警告を抑止しない。"""
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
    """実在するがGitディレクトリではないGIT_DIRでは128の警告を抑止する。"""
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
    """cwdリポジトリ外に解決されるシンボリックリンク先はgitignore判定対象外として残る。"""
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
    """シンボリックリンクディレクトリ自身が.gitignore対象なら配下は走査されない。"""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "child.py").write_text("x = 1\n")
    (tmp_path / "ignored_link").symlink_to("real", target_is_directory=True)
    (tmp_path / "regular.py").write_text("y = 2\n")
    # ファイル形式パターンで早期スキップが成立する
    (tmp_path / ".gitignore").write_text("ignored_link\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        all_files = pyfltr.command.targets.expand_all_files([], config)
        py_files = pyfltr.command.targets.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in py_files}
        assert "regular.py" in names
        # symlink越しのパスは早期スキップにより列挙されない
        assert not any("ignored_link" in str(p) for p in py_files)
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_deduplicates_realpath(tmp_path: pathlib.Path) -> None:
    """同一実体ファイルへ複数パスから到達した場合、実体パス単位で重複排除されソートされる。"""
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
        # シンボリックリンク名がアルファベット順で先（AAA.md → bbb.md）
        (["AAA.md"], "bbb.md"),
        # シンボリックリンク名がアルファベット順で後（zzz.md → bbb.md）
        (["zzz.md"], "bbb.md"),
        # シンボリックリンク名と実体名が同階層に混在（複数symlink + 実体）
        (["AAA.md", "zzz.md"], "bbb.md"),
    ],
)
def test_expand_all_files_prefers_non_symlink_target(tmp_path: pathlib.Path, symlink_names: list[str], real_name: str) -> None:
    """同一実体に複数パスが紐付く場合、非シンボリックリンクのパスを優先して残す。

    `iterdir()`の返却順差（Linux ext4は作成順・Windows NTFSはアルファベット順）に
    左右されず、prettier等の末端シンボリックリンク明示指定エラーを決定論的に回避する。
    """
    # `iterdir()`の作成順走査でsymlinkが先に列挙される入力順を強制するため、
    # 実体ファイルより先にシンボリックリンクを作成する。
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
    """`filter_by_globs`が正しくフィルタリングする。"""
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
    """`only_failed_targets=None`のとき、コマンドに関係なくNoneを返す。"""
    result = pyfltr.command.targets.pick_targets(None, "ruff-check")
    assert result is None


def test_pick_targets_returns_entry_for_matching_command(tmp_path: pathlib.Path) -> None:
    """`only_failed_targets`dictにコマンドが含まれるとき、対応するToolTargetsを返す。"""
    file_a = tmp_path / "a.py"
    targets = {"ruff-check": pyfltr.command.only_failed.ToolTargets.with_files([file_a])}
    result = pyfltr.command.targets.pick_targets(targets, "ruff-check")
    assert result is not None
    assert result.mode == "files"
    assert result.files == (file_a,)


def test_pick_targets_returns_none_for_missing_command() -> None:
    """`only_failed_targets`dictにコマンドが含まれないときNoneを返す。"""
    targets: dict[str, pyfltr.command.only_failed.ToolTargets] = {}
    result = pyfltr.command.targets.pick_targets(targets, "mypy")
    assert result is None
