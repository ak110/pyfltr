"""command.py のコアテスト。

dispatcher・共通処理・環境変数・コマンドライン解決・``_run_subprocess``・
キャッシュ・only_failed・プロセス管理を検証する。
"""

# pylint: disable=protected-access,too-many-lines,duplicate-code

import argparse
import contextlib
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
import time
import typing

import psutil
import pytest

import pyfltr.cache
import pyfltr.command
import pyfltr.config
import pyfltr.only_failed
import pyfltr.warnings_
from tests import conftest as _testconf


def test_build_subprocess_env_sets_supply_chain_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """サプライチェーン対策用の環境変数が既定値で注入される。"""
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    monkeypatch.delenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", raising=False)

    config = pyfltr.config.create_default_config()
    env = pyfltr.command._build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 day"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "1440"


def test_build_subprocess_env_sets_python_utf8_mode() -> None:
    """サブプロセスはPython UTF-8モードで動く。"""
    config = pyfltr.config.create_default_config()
    env = pyfltr.command._build_subprocess_env(config, "pytest")

    assert env["PYTHONUTF8"] == "1"


def test_build_subprocess_env_preserves_existing_supply_chain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ユーザーが既に環境変数を設定している場合は既存値を尊重する。"""
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "10080")

    config = pyfltr.config.create_default_config()
    env = pyfltr.command._build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 week"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "10080"


def test_resolve_js_commandline_pnpx_with_textlint_packages() -> None:
    """pnpx runner では textlint-packages が --package で展開される。

    textlint 本体の spec は `_JS_TOOL_PNPX_PACKAGE_SPEC` によって
    既知バグのあるバージョンを除外した形で指定される。
    """
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing", "textlint-rule-ja-no-abusage"]

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == [
        "--package",
        "textlint@<15.5.3 || >15.5.3",
        "--package",
        "textlint-rule-preset-ja-technical-writing",
        "--package",
        "textlint-rule-ja-no-abusage",
        "textlint",
    ]


def test_resolve_js_commandline_pnpx_textlint_default_excludes_buggy_version() -> None:
    """pnpx runner の既定状態でも textlint 15.5.3 が除外 spec で指定される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == [
        "--package",
        "textlint@<15.5.3 || >15.5.3",
        "--package",
        "textlint-rule-preset-ja-technical-writing",
        "--package",
        "textlint-rule-preset-jtf-style",
        "--package",
        "textlint-rule-ja-no-abusage",
        "textlint",
    ]


def test_resolve_js_commandline_pnpx_markdownlint_unchanged() -> None:
    """markdownlint は除外対象外で、従来どおり bin 名がそのまま渡される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("markdownlint", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == ["--package", "markdownlint-cli2", "markdownlint-cli2"]


def test_resolve_js_commandline_pnpm_ignores_packages() -> None:
    """pnpm runner では textlint-packages は無視される (package.json 側で管理前提)。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "textlint"]


def test_resolve_js_commandline_markdownlint_uses_cli2_binary() -> None:
    """markdownlint コマンドの実体は markdownlint-cli2。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command._resolve_js_commandline("markdownlint", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "markdownlint-cli2"]


def test_resolve_js_commandline_pnpx_eslint() -> None:
    """pnpx runner で eslint が通常通り (bin 名 = パッケージ名) 解決される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("eslint", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == ["--package", "eslint", "eslint"]


def test_resolve_js_commandline_pnpx_prettier() -> None:
    """pnpx runner で prettier が通常通り解決される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("prettier", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == ["--package", "prettier", "prettier"]


def test_resolve_js_commandline_pnpx_biome_uses_scoped_package() -> None:
    """pnpx runner で biome はスコープ付きパッケージ @biomejs/biome で解決される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("biome", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    # --package には @biomejs/biome、bin 名は biome
    assert prefix == ["--package", "@biomejs/biome", "biome"]


def test_resolve_js_commandline_pnpm_prettier() -> None:
    """pnpm runner で prettier が pnpm exec prettier になる。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command._resolve_js_commandline("prettier", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "prettier"]


def test_resolve_js_commandline_pnpm_biome() -> None:
    """pnpm runner で biome が pnpm exec biome になる (スコープ無効)。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command._resolve_js_commandline("biome", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "biome"]


def test_resolve_js_commandline_npx() -> None:
    """npx runner では -p でパッケージを指定する。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "npx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert pathlib.PurePath(path).stem == "npx"
    assert prefix == [
        "--no-install",
        "-p",
        "textlint-rule-preset-ja-technical-writing",
        "--",
        "textlint",
    ]


def test_resolve_js_commandline_direct_missing_raises(tmp_path: pathlib.Path) -> None:
    """direct runner で node_modules/.bin/<cmd> が無ければ FileNotFoundError。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "direct"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(FileNotFoundError):
            pyfltr.command._resolve_js_commandline("textlint", config)
    finally:
        os.chdir(original_cwd)


def test_resolve_js_commandline_direct_found(tmp_path: pathlib.Path) -> None:
    """direct runner で node_modules/.bin/<cmd> があれば path を返す。"""
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "textlint").write_text("#!/bin/sh\necho stub\n")

    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "direct"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)
        assert path.endswith("textlint")
        assert not prefix
    finally:
        os.chdir(original_cwd)


def test_execute_command_direct_missing_returns_failed_result(tmp_path: pathlib.Path) -> None:
    """js-runner=direct で実行ファイル不在時、例外でなく resolution_failed CommandResult を返す。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "direct"
    config.values["textlint"] = True

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        result = pyfltr.command.execute_command(
            "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target])
        )
        assert result.status == "resolution_failed"
        assert result.has_error is True
        assert "node_modules" in result.output
    finally:
        os.chdir(original_cwd)


def test_run_subprocess_file_not_found_returns_127() -> None:
    """存在しない実行ファイルを指定しても例外を送出せず rc=127 を返す。"""
    result = pyfltr.command._run_subprocess(
        ["this-command-definitely-does-not-exist-xyz-1234"],
        env={"PATH": "/nonexistent"},
    )
    assert result.returncode == 127
    assert "見つかりません" in result.stdout


def test_build_subprocess_env_npm_config_actually_effective(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """注入した NPM_CONFIG_MINIMUM_RELEASE_AGE が実際に npm 互換ツールに反映されることを確認する。

    環境変数名が typo したり、仕様変更で効かなくなったりした場合に検知する。
    既定値 (1440) は実行環境のグローバル設定と区別できないため、
    ユーザー既定値優先 (setdefault) の動作を利用して非標準値 4321 を注入し検証する。

    検証には npm を使用する。pnpm はインストール方法やバージョンにより
    NPM_CONFIG_* 環境変数の読み取り動作が不安定なため（pnpm config get が
    env var を無視するケースがある）、npm の config get で代替する。
    npm は NPM_CONFIG_* 規約の本家であり、動作が安定している。
    """
    # npm の設定ファイル読込を避けるため、隔離した HOME を用意する。
    # XDG_CONFIG_HOME も明示的に隔離してグローバル設定の干渉を排除する。
    original_home = pathlib.Path(os.environ.get("HOME") or os.environ["USERPROFILE"])
    mise_config = original_home / ".config" / "mise" / "config.toml"
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", str(mise_config))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    # 非標準値を設定し、_build_subprocess_env がそのまま通すことを利用する。
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "4321")

    config = pyfltr.config.create_default_config()
    env = pyfltr.command._build_subprocess_env(config, "markdownlint")
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "4321"

    # Windows では npm が npm.cmd として提供されるため、shutil.which で完全パスを取得する
    npm_path = shutil.which("npm")
    assert npm_path is not None
    proc = subprocess.run(
        [npm_path, "config", "get", "minimum-release-age"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "4321"


def test_excluded_default_patterns() -> None:
    """DEFAULT_CONFIG["exclude"] が主要パターンに対して正しく動作することを確認する。"""
    config = pyfltr.config.create_default_config()

    # 直接マッチ （ディレクトリ名）
    assert pyfltr.command.excluded(pathlib.Path(".serena"), config)
    assert pyfltr.command.excluded(pathlib.Path(".cursor"), config)
    assert pyfltr.command.excluded(pathlib.Path(".idea"), config)
    assert pyfltr.command.excluded(pathlib.Path(".venv"), config)
    assert pyfltr.command.excluded(pathlib.Path("node_modules"), config)

    # 親ディレクトリマッチ （配下ファイル）
    assert pyfltr.command.excluded(pathlib.Path(".serena/memories/foo.md"), config)
    assert pyfltr.command.excluded(pathlib.Path(".cursor/rules/bar.mdc"), config)
    assert pyfltr.command.excluded(pathlib.Path(".idea/workspace.xml"), config)

    # ワイルドカードパターン （.aider*）
    assert pyfltr.command.excluded(pathlib.Path(".aider.conf.yml"), config)
    assert pyfltr.command.excluded(pathlib.Path(".aider.chat.history.md"), config)

    # 無関係なパスは除外されないこと
    assert not pyfltr.command.excluded(pathlib.Path("pyfltr/config.py"), config)
    assert not pyfltr.command.excluded(pathlib.Path("tests/command_test.py"), config)
    assert not pyfltr.command.excluded(pathlib.Path("README.md"), config)

    # 戻り値は (設定キー, 一致パターン) のタプル
    config.values["exclude"] = []
    config.values["extend-exclude"] = ["sample.py"]
    assert pyfltr.command.excluded(pathlib.Path("sample.py"), config) == ("extend-exclude", "sample.py")


def test_excluded_disabled_by_empty_config() -> None:
    """exclude/extend-excludeが空の場合、全パスが除外されないことを確認する（--no-exclude相当）。"""
    config = pyfltr.config.create_default_config()
    config.values["exclude"] = []
    config.values["extend-exclude"] = []

    # 通常は除外されるパスが除外されないこと
    assert not pyfltr.command.excluded(pathlib.Path(".venv"), config)
    assert not pyfltr.command.excluded(pathlib.Path("node_modules"), config)
    assert not pyfltr.command.excluded(pathlib.Path(".serena/memories/foo.md"), config)


def test_expand_all_files_respects_gitignore(tmp_path: pathlib.Path) -> None:
    """.gitignore に記載されたファイルが expand_all_files から除外される。"""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("x = 2\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.create_default_config()
        all_files = pyfltr.command.expand_all_files([], config)
        result = pyfltr.command.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in result}
        assert "main.py" in names
        assert "ignored.py" not in names
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_gitignore_disabled(tmp_path: pathlib.Path) -> None:
    """respect-gitignore = false の場合、.gitignore によるフィルタリングが無効になる。"""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("x = 2\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.create_default_config()
        config.values["respect-gitignore"] = False
        all_files = pyfltr.command.expand_all_files([], config)
        result = pyfltr.command.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in result}
        assert "main.py" in names
        assert "ignored.py" in names
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_no_git_repo(tmp_path: pathlib.Path) -> None:
    """git リポジトリ外でも正常に動作する。"""
    (tmp_path / "main.py").write_text("x = 1\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.create_default_config()
        all_files = pyfltr.command.expand_all_files([], config)
        result = pyfltr.command.filter_by_globs(all_files, ["*.py"])
        names = {p.name for p in result}
        assert "main.py" in names
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_warns_excluded_file(tmp_path: pathlib.Path, caplog) -> None:
    """直接指定されたファイルがexclude設定で除外された場合に警告が出る。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.create_default_config()
        config.values["extend-exclude"] = ["sample.py"]
        with caplog.at_level(logging.WARNING):
            result = pyfltr.command.expand_all_files([target], config)
        assert len(result) == 0
        assert "除外設定により無視されました" in caplog.text
        assert 'extend-exclude="sample.py"' in caplog.text
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_warns_gitignored_file(tmp_path: pathlib.Path, caplog) -> None:
    """直接指定されたファイルが .gitignore で除外された場合に警告が出る。"""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    target = tmp_path / "ignored.py"
    target.write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.create_default_config()
        with caplog.at_level(logging.WARNING):
            result = pyfltr.command.expand_all_files([target], config)
        assert len(result) == 0
        assert ".gitignore により無視されました" in caplog.text
    finally:
        os.chdir(original_cwd)


def test_filter_by_globs() -> None:
    """filter_by_globs が正しくフィルタリングする。"""
    files = [
        pathlib.Path("main.py"),
        pathlib.Path("test_main.py"),
        pathlib.Path("README.md"),
        pathlib.Path("style.css"),
    ]
    assert pyfltr.command.filter_by_globs(files, ["*.py"]) == [
        pathlib.Path("main.py"),
        pathlib.Path("test_main.py"),
    ]
    assert pyfltr.command.filter_by_globs(files, ["*.md", "*.css"]) == [
        pathlib.Path("README.md"),
        pathlib.Path("style.css"),
    ]
    assert pyfltr.command.filter_by_globs(files, ["*.rs"]) == []


def test_build_auto_args_pylint_pydantic() -> None:
    """pylint-pydantic=true の場合に自動引数が挿入される。"""
    config = pyfltr.config.create_default_config()
    result = pyfltr.command._build_auto_args("pylint", config, [])
    assert "--load-plugins=pylint_pydantic" in result


def test_build_auto_args_mypy_unused_awaitable() -> None:
    """mypy-unused-awaitable=true の場合に自動引数が挿入される。"""
    config = pyfltr.config.create_default_config()
    result = pyfltr.command._build_auto_args("mypy", config, [])
    assert "--enable-error-code=unused-awaitable" in result


def test_build_auto_args_disabled() -> None:
    """自動オプションを false にすると引数が挿入されない。"""
    config = pyfltr.config.create_default_config()
    config.values["pylint-pydantic"] = False
    result = pyfltr.command._build_auto_args("pylint", config, [])
    assert "--load-plugins=pylint_pydantic" not in result


def test_build_auto_args_dedup_with_user_args() -> None:
    """ユーザーが既に同じ引数を指定している場合はスキップする。"""
    config = pyfltr.config.create_default_config()
    user_args = ["--load-plugins=pylint_pydantic", "--jobs=4"]
    result = pyfltr.command._build_auto_args("pylint", config, user_args)
    assert "--load-plugins=pylint_pydantic" not in result


def test_build_auto_args_no_match() -> None:
    """AUTO_ARGS に定義されていないコマンドは空リストを返す。"""
    config = pyfltr.config.create_default_config()
    result = pyfltr.command._build_auto_args("ruff-check", config, [])
    assert not result


def test_auto_args_included_in_commandline(mocker, tmp_path: pathlib.Path) -> None:
    """execute_command の結果コマンドラインに自動引数が含まれる。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["pylint"], returncode=0, stdout="")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["pylint"] = True
    result = pyfltr.command.execute_command("pylint", _testconf.make_args(), _testconf.make_execution_context(config, [target]))
    assert "--load-plugins=pylint_pydantic" in result.commandline


# --- bin-runner テスト ---


def test_resolve_bin_commandline_direct_found(mocker) -> None:
    """directモードでwhichが成功した場合、解決されたパスを返す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/shellcheck")

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "direct"

    path, prefix = pyfltr.command._resolve_bin_commandline("shellcheck", config)

    assert path == "/usr/local/bin/shellcheck"
    assert not prefix


def test_resolve_bin_commandline_direct_not_found(mocker) -> None:
    """directモードでwhichが失敗した場合、FileNotFoundErrorを送出する。"""
    mocker.patch("shutil.which", return_value=None)

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "direct"

    with pytest.raises(FileNotFoundError, match="shellcheck"):
        pyfltr.command._resolve_bin_commandline("shellcheck", config)


def test_resolve_bin_commandline_mise_success(mocker) -> None:
    """miseモードでツールが利用可能な場合、mise exec形式のコマンドラインを返す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec", "shellcheck@latest", "--", "shellcheck", "--version"],
            returncode=0,
            stdout="shellcheck 0.9.0",
            stderr="",
        ),
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = pyfltr.command._resolve_bin_commandline("shellcheck", config)

    assert path == "mise"
    assert prefix == ["exec", "shellcheck@latest", "--", "shellcheck"]


def test_resolve_bin_commandline_mise_custom_version(mocker) -> None:
    """miseモードでカスタムバージョンが指定された場合のテスト。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise"],
            returncode=0,
            stdout="",
            stderr="",
        ),
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["shellcheck-version"] = "0.9.0"

    path, prefix = pyfltr.command._resolve_bin_commandline("shellcheck", config)

    assert path == "mise"
    assert prefix == ["exec", "shellcheck@0.9.0", "--", "shellcheck"]


def test_resolve_bin_commandline_mise_not_installed_fallback(mocker) -> None:
    """miseモードでmise未導入かつ対象バイナリがPATHにある場合、direct相当でフォールバックする。"""

    def fake_which(name: str) -> str | None:
        if name == "mise":
            return None
        if name == "actionlint":
            return "/usr/local/bin/actionlint"
        return None

    mocker.patch("shutil.which", side_effect=fake_which)

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = pyfltr.command._resolve_bin_commandline("actionlint", config)

    assert path == "/usr/local/bin/actionlint"
    assert not prefix


def test_resolve_bin_commandline_mise_not_installed_no_fallback(mocker) -> None:
    """miseモードでmise未導入かつ対象バイナリもPATHに無い場合、FileNotFoundErrorを送出する。"""
    mocker.patch("shutil.which", return_value=None)

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError, match="actionlint"):
        pyfltr.command._resolve_bin_commandline("actionlint", config)


def test_resolve_bin_commandline_glab_ci_lint_mise(mocker) -> None:
    """glab-ci-lint は mise バックエンド経由で glab バイナリを解決する。

    ``ci lint`` サブコマンドは args 既定値側に持たせる設計のため、bin-runner 解決の
    プレフィクスにはサブコマンドが含まれない (commandline 組み立て段で付与される)。
    """
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = pyfltr.command._resolve_bin_commandline("glab-ci-lint", config)

    assert path == "mise"
    assert prefix == ["exec", "glab@latest", "--", "glab"]
    # args 既定値にサブコマンドが含まれていることを確認 (明示 path 指定時にも有効化させるため)
    assert config["glab-ci-lint-args"] == ["ci", "lint"]


def test_resolve_bin_commandline_glab_ci_lint_direct(mocker) -> None:
    """direct モードでは PATH 上の glab バイナリを解決する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/glab")

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "direct"

    path, prefix = pyfltr.command._resolve_bin_commandline("glab-ci-lint", config)

    assert path == "/usr/local/bin/glab"
    assert not prefix


def test_resolve_bin_commandline_mise_tool_not_installed(mocker) -> None:
    """miseモードでツールが未インストールの場合、FileNotFoundErrorをstderr付きで送出する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise"],
            returncode=1,
            stdout="",
            stderr="tool not found",
        ),
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError, match="tool not found"):
        pyfltr.command._resolve_bin_commandline("ec", config)


def test_resolve_bin_commandline_mise_untrusted_auto_trust_success(mocker) -> None:
    """未信頼エラー → trust成功 → 再チェック成功の3段で最終的に通常成功扱いになる。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        side_effect=[
            # 1回目の事前チェック: config未信頼で失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
            ),
            # mise trust --yes --all: 成功
            subprocess.CompletedProcess(
                ["mise", "trust", "--yes", "--all"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            # 2回目の事前チェック（リトライ）: 成功
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=0,
                stdout="shellcheck 0.9.0",
                stderr="",
            ),
        ],
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    path, prefix = pyfltr.command._resolve_bin_commandline("shellcheck", config)

    assert path == "mise"
    assert prefix == ["exec", "shellcheck@latest", "--", "shellcheck"]
    # 事前チェック → trust → リトライの3回が実際に発生したことを確認
    assert mock_run.call_count == 3


def test_resolve_bin_commandline_mise_untrusted_auto_trust_disabled(mocker) -> None:
    """mise-auto-trust=False のとき trust が呼ばれず、stderr含むエラーメッセージで失敗する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec"],
            returncode=1,
            stdout="",
            stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
        ),
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = False

    with pytest.raises(FileNotFoundError, match="not trusted"):
        pyfltr.command._resolve_bin_commandline("shellcheck", config)

    # trust コマンドは呼ばれていないことを確認（subprocess.run の呼び出しは1回のみ）
    assert mock_run.call_count == 1


def test_resolve_bin_commandline_mise_other_error_no_retry(mocker) -> None:
    """未信頼以外のエラー（plugin not found等）ではtrustを呼ばずそのまま失敗する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec"],
            returncode=1,
            stdout="",
            stderr="mise ERROR plugin not found: shellcheck",
        ),
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="plugin not found"):
        pyfltr.command._resolve_bin_commandline("shellcheck", config)

    # trust コマンドは呼ばれていないことを確認（subprocess.run の呼び出しは1回のみ）
    assert mock_run.call_count == 1


def test_resolve_bin_commandline_mise_untrusted_auto_trust_retry_failure(mocker) -> None:
    """trust後の再チェックも失敗する場合、リトライが1回で打ち切られ通常失敗扱いになる。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        side_effect=[
            # 1回目の事前チェック: config未信頼で失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
            ),
            # mise trust --yes --all: 成功
            subprocess.CompletedProcess(
                ["mise", "trust", "--yes", "--all"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            # 2回目の事前チェック（リトライ）: 再度失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR some other failure after trust",
            ),
        ],
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="some other failure after trust"):
        pyfltr.command._resolve_bin_commandline("shellcheck", config)


def test_resolve_bin_commandline_mise_untrusted_auto_trust_trust_failure(mocker) -> None:
    """mise trust コマンド自体が失敗した場合、trust.stderr を含むエラーで即座に失敗する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        side_effect=[
            # 事前チェック: config未信頼で失敗
            subprocess.CompletedProcess(
                ["mise", "exec"],
                returncode=1,
                stdout="",
                stderr="mise ERROR Config files in /path/to/mise.toml are not trusted.",
            ),
            # mise trust --yes --all: 失敗（権限不足等）
            subprocess.CompletedProcess(
                ["mise", "trust", "--yes", "--all"],
                returncode=1,
                stdout="",
                stderr="mise ERROR permission denied: /path/to/mise.toml",
            ),
        ],
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="permission denied"):
        pyfltr.command._resolve_bin_commandline("shellcheck", config)


def test_failed_resolution_result() -> None:
    """_failed_resolution_resultが解決失敗専用のCommandResultを返す。"""
    command_info = pyfltr.config.CommandInfo(type="linter")

    result = pyfltr.command._failed_resolution_result("shellcheck", command_info, "ツールが見つかりません: shellcheck", files=3)

    assert result.returncode == 1
    assert result.has_error is True
    assert result.resolution_failed is True
    assert result.status == "resolution_failed"
    assert result.files == 3
    assert "shellcheck" in result.output
    assert result.command == "shellcheck"
    assert result.elapsed == 0.0


def test_pass_filenames_false_omits_targets(mocker, tmp_path: pathlib.Path) -> None:
    """pass-filenames=falseの場合、コマンドラインにファイル引数が含まれない。"""
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["tsc"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["tsc"] = True
    # tscはデフォルトでpass-filenames=false
    assert config["tsc-pass-filenames"] is False

    result = pyfltr.command.execute_command("tsc", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    # ファイルパスがコマンドラインに含まれないことを確認
    assert str(target) not in cmdline
    assert result.status == "succeeded"


def test_pass_filenames_true_includes_targets(mocker, tmp_path: pathlib.Path) -> None:
    """pass-filenames=true（既定）の場合、コマンドラインにファイル引数が含まれる。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True
    result = pyfltr.command.execute_command(
        "ruff-check", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    # ファイルパスがコマンドラインに含まれることを確認
    assert str(target) in cmdline
    assert result.status == "succeeded"


def test_bin_tool_spec_all_tools_defined() -> None:
    """_BIN_TOOL_SPECに全bin系ツールが定義されている。"""
    expected_tools = {
        # 既存のネイティブバイナリツール
        "ec",
        "shellcheck",
        "shfmt",
        "actionlint",
        "glab-ci-lint",
        "taplo",
        "hadolint",
        "gitleaks",
        # cargo 系・dotnet 系も bin-runner 経路へ統合済み（mise backend 経由で解決）。
        "cargo-fmt",
        "cargo-clippy",
        "cargo-check",
        "cargo-test",
        "cargo-deny",
        "dotnet-format",
        "dotnet-build",
        "dotnet-test",
    }
    assert set(pyfltr.command._BIN_TOOL_SPEC.keys()) == expected_tools


def test_bin_tool_spec_structure() -> None:
    """BinToolSpecのフィールドが正しく設定されている。"""
    spec = pyfltr.command._BIN_TOOL_SPEC["ec"]
    assert spec.bin_name == "ec"
    assert spec.mise_backend == "editorconfig-checker"
    assert spec.default_version == "latest"

    spec = pyfltr.command._BIN_TOOL_SPEC["shellcheck"]
    assert spec.bin_name == "shellcheck"


def test_command_result_cached_defaults() -> None:
    """CommandResult の新フィールド cached/cached_from の既定値テスト。"""
    result = pyfltr.command.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        has_error=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    assert result.cached is False
    assert result.cached_from is None


def test_execute_command_cache_hit_skips_subprocess(mocker, tmp_path: pathlib.Path) -> None:
    """キャッシュヒット時は subprocess 実行をスキップして cached=True を返す。"""
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    cache_root = tmp_path / ".cache"
    store = pyfltr.cache.CacheStore(cache_root=cache_root)

    mock_run = mocker.patch("pyfltr.command._run_subprocess")

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-path"] = "/bin/true"  # js-runner を使わず path 指定で解決を単純化

    # 1 回目: キャッシュミスで subprocess 実行
    mock_run.return_value = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="ok")
    result1 = pyfltr.command.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01ABCDEFGH"),
    )
    assert mock_run.call_count == 1
    assert result1.cached is False

    # 2 回目: キャッシュヒットで subprocess 実行されない
    result2 = pyfltr.command.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01XYZ"),
    )
    assert mock_run.call_count == 1  # 増えていない
    assert result2.cached is True
    assert result2.cached_from == "01ABCDEFGH"


def test_execute_command_non_cacheable_skips_cache(mocker, tmp_path: pathlib.Path) -> None:
    """cacheable=False のツール (mypy 等) はキャッシュに書かれない。"""
    target = tmp_path / "foo.py"
    target.write_text("x = 1\n")
    cache_root = tmp_path / ".cache"
    store = pyfltr.cache.CacheStore(cache_root=cache_root)

    mocker.patch(
        "pyfltr.command._run_subprocess",
        return_value=subprocess.CompletedProcess(["mypy"], returncode=0, stdout=""),
    )

    config = pyfltr.config.create_default_config()
    config.values["mypy"] = True

    pyfltr.command.execute_command(
        "mypy",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01ABCDEFGH"),
    )
    # mypy は cacheable=False のため、キャッシュエントリは作られない
    assert not list(cache_root.rglob("*.json"))


def test_execute_command_only_failed_targets_files_override(mocker, tmp_path: pathlib.Path) -> None:
    """``only_failed_targets`` に ToolTargets.with_files を渡すと ``all_files`` の代わりにその集合が対象になる。"""
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.py"
    file_a.write_text("x = 1\n")
    file_b.write_text("y = 2\n")

    mock_run = mocker.patch(
        "pyfltr.command._run_subprocess",
        return_value=subprocess.CompletedProcess(["ruff"], returncode=0, stdout=""),
    )

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True

    result = pyfltr.command.execute_command(
        "ruff-check",
        _testconf.make_args(),
        _testconf.make_execution_context(
            config, [file_a, file_b], only_failed_targets=pyfltr.only_failed.ToolTargets.with_files([file_b])
        ),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_b) in cmdline
    assert str(file_a) not in cmdline
    # CommandResult.target_files も ToolTargets ベースに絞られる
    assert result.target_files == [file_b]


def test_execute_command_only_failed_targets_fallback_uses_all_files(mocker, tmp_path: pathlib.Path) -> None:
    """``ToolTargets.fallback_default()`` なら既定の ``all_files`` で実行される。"""
    file_a = tmp_path / "a.py"
    file_a.write_text("x = 1\n")

    mock_run = mocker.patch(
        "pyfltr.command._run_subprocess",
        return_value=subprocess.CompletedProcess(["ruff"], returncode=0, stdout=""),
    )

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True

    pyfltr.command.execute_command(
        "ruff-check",
        _testconf.make_args(),
        _testconf.make_execution_context(
            config, [file_a], only_failed_targets=pyfltr.only_failed.ToolTargets.fallback_default()
        ),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_a) in cmdline


def test_execute_command_only_failed_targets_none_uses_default(mocker, tmp_path: pathlib.Path) -> None:
    """``only_failed_targets=None`` なら既定の ``all_files`` で実行される（--only-failed 未指定）。"""
    file_a = tmp_path / "a.py"
    file_a.write_text("x = 1\n")

    mock_run = mocker.patch(
        "pyfltr.command._run_subprocess",
        return_value=subprocess.CompletedProcess(["ruff"], returncode=0, stdout=""),
    )

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True

    pyfltr.command.execute_command(
        "ruff-check",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [file_a], only_failed_targets=None),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_a) in cmdline


def test_pick_targets_none_when_targets_is_none() -> None:
    """``only_failed_targets=None`` のとき、コマンドに関係なく None を返す。"""
    result = pyfltr.command.pick_targets(None, "ruff-check")
    assert result is None


def test_pick_targets_returns_entry_for_matching_command(tmp_path: pathlib.Path) -> None:
    """``only_failed_targets`` dict にコマンドが含まれるとき、対応する ToolTargets を返す。"""
    file_a = tmp_path / "a.py"
    targets = {"ruff-check": pyfltr.only_failed.ToolTargets.with_files([file_a])}
    result = pyfltr.command.pick_targets(targets, "ruff-check")
    assert result is not None
    assert result.mode == "files"
    assert result.files == (file_a,)


def test_pick_targets_returns_none_for_missing_command() -> None:
    """``only_failed_targets`` dict にコマンドが含まれないとき None を返す。"""
    targets: dict[str, pyfltr.only_failed.ToolTargets] = {}
    result = pyfltr.command.pick_targets(targets, "mypy")
    assert result is None


class _FakePopen:
    """``subprocess.Popen`` を差し替えるための最小スタブ。

    ``_run_subprocess`` のテスト用。Popen の with 文経由での利用と stdout 逐次読み込み・
    wait() までを満たす最小限の振る舞いを提供する。起動引数はクラス変数
    ``last_args_holder`` のリスト内に追記する (None 判定を避けて pylint の型縮めに頼らない)。
    """

    last_args_holder: list[list[str]] = []

    def __init__(self, args, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs  # noqa
        _FakePopen.last_args_holder.append(list(args))
        self.returncode = 0
        self.stdout: typing.Iterator[str] = iter([])

    def __enter__(self):  # type: ignore[no-untyped-def]
        """with 文のエントリー。"""
        return self

    def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        """with 文のイグジット。"""
        del exc_type, exc, tb  # noqa
        return False

    def wait(self):  # type: ignore[no-untyped-def]
        """プロセス終了待ち。ダミーで直ちに returncode を返す。"""
        return self.returncode


def test_run_subprocess_resolves_command_via_shutil_which(mocker) -> None:
    """``commandline[0]`` が ``shutil.which`` で解決されて Popen に渡る。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.shutil.which", return_value="/resolved/pre-commit")
    mocker.patch("pyfltr.command.subprocess.Popen", _FakePopen)

    pyfltr.command._run_subprocess(["pre-commit", "run", "--all-files"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [["/resolved/pre-commit", "run", "--all-files"]]


def test_run_subprocess_keeps_original_name_when_unresolved(mocker) -> None:
    """``shutil.which`` が None なら元のコマンド名のまま Popen に渡る。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.shutil.which", return_value=None)
    mocker.patch("pyfltr.command.subprocess.Popen", _FakePopen)

    pyfltr.command._run_subprocess(["missing-tool", "arg"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [["missing-tool", "arg"]]


def test_run_subprocess_resolves_via_env_path(mocker, tmp_path: pathlib.Path, monkeypatch) -> None:
    """``os.environ["PATH"]`` では見えず ``env["PATH"]`` にだけある実行ファイルが解決される。

    解決探索対象 PATH と Popen へ渡す ``env["PATH"]`` の一致要件に対するリグレッション防止。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Windows の shutil.which は PATHEXT に列挙された拡張子で実行ファイルを判定するため、
    # ダミー実行ファイル名を `.bat` にする (POSIX では実行属性 0o755 で判定される)。
    # 本テストの主眼は env["PATH"] 経由での解決可否であり、拡張子/実行属性の違いは付随的。
    if os.name == "nt":
        target = bin_dir / "faketool.bat"
        target.write_text("")
    else:
        target = bin_dir / "faketool"
        target.write_text("")
        target.chmod(0o755)

    # os.environ の PATH からは bin_dir を除外する (env["PATH"] 経由で解決することの検証)
    monkeypatch.setenv("PATH", "/nonexistent-pyfltr-test-path")

    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.subprocess.Popen", _FakePopen)

    pyfltr.command._run_subprocess(["faketool"], {"PATH": str(bin_dir)})

    # 解決されたパスが渡ること (先頭要素が /tmp/.../bin/faketool* を指す)
    assert len(_FakePopen.last_args_holder) == 1
    resolved = pathlib.Path(_FakePopen.last_args_holder[0][0])
    assert resolved.name.startswith("faketool")
    assert resolved.parent == bin_dir


def test_run_subprocess_does_not_mutate_commandline(mocker) -> None:
    """呼び出し側の ``commandline`` リストは書き換えない (retry_command 等に影響するため)。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.shutil.which", return_value="/resolved/tool")
    mocker.patch("pyfltr.command.subprocess.Popen", _FakePopen)

    original = ["tool", "arg"]
    pyfltr.command._run_subprocess(original, {"PATH": "/usr/bin"})

    assert original == ["tool", "arg"]


def test_get_env_path_windows_uses_case_insensitive_key(monkeypatch) -> None:
    """Windows (``os.name == "nt"``) では ``Path`` キーも ``PATH`` として採用される。"""
    monkeypatch.setattr("pyfltr.command.os.name", "nt")
    assert pyfltr.command._get_env_path({"Path": "/tmp/bin"}) == "/tmp/bin"
    assert pyfltr.command._get_env_path({"path": "/tmp/bin"}) == "/tmp/bin"
    # PATH 大文字が存在する場合も取れる
    assert pyfltr.command._get_env_path({"PATH": "/tmp/bin"}) == "/tmp/bin"


def test_get_env_path_posix_strict_key(monkeypatch) -> None:
    """POSIX では ``env.get("PATH")`` のみを使い、``Path`` キーは採用しない。

    ``env={"Path": "/tmp/bin", "PATH": "/usr/bin"}`` で解決側と Popen 実行時側の PATH が
    不一致になる事故を防ぐ設計。
    """
    monkeypatch.setattr("pyfltr.command.os.name", "posix")
    assert pyfltr.command._get_env_path({"Path": "/tmp/bin"}) is None
    assert pyfltr.command._get_env_path({"PATH": "/usr/bin"}) == "/usr/bin"
    # 両方あっても PATH のみを採用する
    assert pyfltr.command._get_env_path({"Path": "/tmp/bin", "PATH": "/usr/bin"}) == "/usr/bin"


def _spawn_parent_with_child(script: str) -> tuple[subprocess.Popen[str], int, int]:
    """Python スクリプトを subprocess として起動し親pidと子pidを取得する。

    スクリプトは最初の 1 行に自身と子の pid を空白区切りで print する契約。
    Popen は ``start_new_session=True`` で起動する（本番と同じ条件）。
    """
    # pylint: disable=consider-using-with
    # テスト対象の ``_active_processes`` へ外から登録するため、``with`` 構文では
    # スコープ外で proc を扱えない。各テストの finally で解放する。
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    parent_pid_str, child_pid_str = line.split()
    return proc, int(parent_pid_str), int(child_pid_str)


def _wait_gone(pids: list[int], *, timeout: float) -> list[int]:
    """``pids`` が全て消滅するまで最大 ``timeout`` 秒待つ。残存する pid を返す。

    init を持たないコンテナー環境では親 reap が行われず zombie が残存するため、
    zombie 状態は消滅扱いとする（プロセスツリーは既に停止しており、
    ``terminate_active_processes`` の責務は果たされている）。
    """

    def _is_alive(pid: int) -> bool:
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = [pid for pid in pids if _is_alive(pid)]
        if not alive:
            return []
        time.sleep(0.05)
    return [pid for pid in pids if _is_alive(pid)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX 前提の killpg 経路を検証する")
def test_terminate_active_processes_kills_grandchild() -> None:
    """``terminate_active_processes`` が孫プロセスまで確実に停止する。

    Popen 子が更にサブプロセスを fork する pytest-xdist 相当の構造で、
    ``start_new_session=True`` 相当の pgid 分離により SIGTERM が全体へ届くことを検証する。
    """
    script = textwrap.dedent(
        """
        import os, time
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            # child: pid を pipe へ書き、あとは待機する（stdout へは書かない）。
            os.close(r)
            os.write(w, str(os.getpid()).encode())
            os.close(w)
            while True:
                time.sleep(1)
        else:
            # 親: child の pid を読み取り、自身と child の pid を 1 行にまとめて出力する。
            os.close(w)
            child_pid = int(os.read(r, 64).decode())
            os.close(r)
            print(f"{os.getpid()} {child_pid}", flush=True)
            while True:
                time.sleep(1)
        """
    )
    proc, parent_pid, child_pid = _spawn_parent_with_child(script)
    try:
        with pyfltr.command._active_processes_lock:
            pyfltr.command._active_processes.append(proc)
        assert psutil.pid_exists(parent_pid)
        assert psutil.pid_exists(child_pid)

        pyfltr.command.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([parent_pid, child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかった pid: {remaining}"
    finally:
        with pyfltr.command._active_processes_lock:
            if proc in pyfltr.command._active_processes:
                pyfltr.command._active_processes.remove(proc)
        if proc.poll() is None:
            # POSIX 限定パスのクリーンアップ。Windows では skipif で到達しない。
            # 型チェッカー（pyright / ty）の attr-defined 誤検知は局所コメントで抑止する。
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(proc.pid), 9)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        proc.wait(timeout=2.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 前提の killpg 経路を検証する")
def test_terminate_active_processes_parent_exited_grandchild_remains() -> None:
    """親が先に exit して孫だけが stdout を握り残す構成でも停止できる。

    ``start_new_session=True`` により pgid が proc.pid と一致するため、
    親 reap 後でも ``os.killpg(proc.pid, SIGTERM)`` で孫へ届くことを検証する。
    """
    script = textwrap.dedent(
        """
        import os, time
        pid = os.fork()
        if pid == 0:
            # grandchild 役。stdout を継承したまま待機する。
            while True:
                time.sleep(1)
        else:
            # 親だけが stdout に書き出してすぐ exit。grandchild は stdout を握り続ける。
            print(f"{os.getpid()} {pid}", flush=True)
            os._exit(0)
        """
    )
    proc, _parent_pid, child_pid = _spawn_parent_with_child(script)
    try:
        with pyfltr.command._active_processes_lock:
            pyfltr.command._active_processes.append(proc)
        # 親は速やかに exit する。孫（子）は生存継続。
        proc.wait(timeout=2.0)
        assert psutil.pid_exists(child_pid), "孫プロセスが消えている"

        pyfltr.command.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかった pid: {remaining}"
    finally:
        with pyfltr.command._active_processes_lock:
            if proc in pyfltr.command._active_processes:
                pyfltr.command._active_processes.remove(proc)
        if psutil.pid_exists(child_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(child_pid, 9)


def test_looks_like_glab_host_missing_detects_known_patterns() -> None:
    """ホスト未検出/未認証文言が大小文字差や前後文字を含んでも検出される。"""
    assert pyfltr.command._looks_like_glab_host_missing(
        "Error: none of the git remotes configured for this repository point to a known GitLab host."
    )
    assert pyfltr.command._looks_like_glab_host_missing("you are NOT AUTHENTICATED to glab")
    assert not pyfltr.command._looks_like_glab_host_missing(
        "Error: validation failed: jobs:test config key may not be used with `rules`"
    )
    assert not pyfltr.command._looks_like_glab_host_missing("")


def _make_glab_ci_lint_args() -> argparse.Namespace:
    """``_execute_glab_ci_lint`` で参照される最低限の属性を持つ Namespace を返す。"""
    return argparse.Namespace(verbose=False)


def _make_glab_ci_lint_command_info() -> pyfltr.config.CommandInfo:
    return pyfltr.config.BUILTIN_COMMANDS["glab-ci-lint"]


def test_execute_glab_ci_lint_skips_on_host_missing(mocker, tmp_path: pathlib.Path) -> None:
    """ホスト未検出 stderr を検出したら returncode=None でスキップ扱いに書き換える。"""
    pyfltr.warnings_.clear()
    proc = subprocess.CompletedProcess(
        args=["glab", "ci", "lint"],
        returncode=1,
        stdout="Error: none of the git remotes configured for this repository point to a known GitLab host.\n",
    )
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")

    result = pyfltr.command._execute_glab_ci_lint(
        "glab-ci-lint",
        _make_glab_ci_lint_command_info(),
        ["glab", "ci", "lint"],
        [target],
        {"PATH": os.environ.get("PATH", "")},
        None,
        time.perf_counter(),
        _make_glab_ci_lint_args(),
    )

    assert result.returncode is None
    assert result.status == "skipped"
    assert "スキップしました" in result.output
    warnings = pyfltr.warnings_.collected_warnings()
    assert any(w.get("source") == "glab-ci-lint" for w in warnings)


def test_execute_glab_ci_lint_keeps_failure_for_real_errors(mocker, tmp_path: pathlib.Path) -> None:
    """ホスト未検出以外の非ゼロ終了は failed のまま据え置く。"""
    pyfltr.warnings_.clear()
    proc = subprocess.CompletedProcess(
        args=["glab", "ci", "lint"],
        returncode=1,
        stdout="Error: validation failed: jobs:test config key may not be used with `rules`\n",
    )
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")

    result = pyfltr.command._execute_glab_ci_lint(
        "glab-ci-lint",
        _make_glab_ci_lint_command_info(),
        ["glab", "ci", "lint"],
        [target],
        {"PATH": os.environ.get("PATH", "")},
        None,
        time.perf_counter(),
        _make_glab_ci_lint_args(),
    )

    assert result.returncode == 1
    assert result.status == "failed"


def test_execute_glab_ci_lint_passes_through_success(mocker, tmp_path: pathlib.Path) -> None:
    """正常終了はそのまま succeeded として扱い、ロケール強制環境変数を渡す。"""
    pyfltr.warnings_.clear()
    proc = subprocess.CompletedProcess(
        args=["glab", "ci", "lint"],
        returncode=0,
        stdout="OK\n",
    )
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")

    captured_env: dict[str, str] = {}

    def _capture(*_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        captured_env.update(_args[1])
        return proc

    mocker.patch("pyfltr.command._run_subprocess", side_effect=_capture)

    result = pyfltr.command._execute_glab_ci_lint(
        "glab-ci-lint",
        _make_glab_ci_lint_command_info(),
        ["glab", "ci", "lint"],
        [target],
        {"PATH": os.environ.get("PATH", "")},
        None,
        time.perf_counter(),
        _make_glab_ci_lint_args(),
    )

    assert result.returncode == 0
    assert result.status == "succeeded"
    # ロケール非依存判定のための環境変数強制を確認する。
    assert captured_env["LC_ALL"] == "C"
    assert captured_env["LANG"] == "C"


# --- {command}-runner per-tool 解決のテスト ---


def test_resolve_runner_default_for_existing_bin_tools() -> None:
    """既存の bin-runner 対応 8 ツールおよび cargo / dotnet 系の {command}-runner 既定値は "bin-runner"。"""
    config = pyfltr.config.create_default_config()
    expected_bin = (
        "ec",
        "shellcheck",
        "shfmt",
        "actionlint",
        "glab-ci-lint",
        "taplo",
        "hadolint",
        "gitleaks",
        "cargo-fmt",
        "cargo-clippy",
        "cargo-check",
        "cargo-test",
        "cargo-deny",
        "dotnet-format",
        "dotnet-build",
        "dotnet-test",
    )
    for command in expected_bin:
        runner, source = pyfltr.command.resolve_runner(command, config)
        assert runner == "bin-runner", f"{command} の runner は 'bin-runner' であるべき"
        assert source == "default"


def test_resolve_runner_default_for_js_tools() -> None:
    """JS 系ツール（eslint / prettier / biome / oxlint / tsc / vitest / markdownlint / textlint）の既定は "js-runner"。"""
    config = pyfltr.config.create_default_config()
    for command in ("eslint", "prettier", "biome", "oxlint", "tsc", "vitest", "markdownlint", "textlint"):
        runner, source = pyfltr.command.resolve_runner(command, config)
        assert runner == "js-runner", f"{command} の runner は 'js-runner' であるべき"
        assert source == "default"


def test_resolve_runner_default_for_direct_tools() -> None:
    """typos / yamllint / Python 系ツールの既定は "direct"。"""
    config = pyfltr.config.create_default_config()
    for command in ("typos", "yamllint", "mypy", "pylint", "pyright", "ty", "ruff-check", "ruff-format", "pytest", "uv-sort"):
        runner, source = pyfltr.command.resolve_runner(command, config)
        assert runner == "direct", f"{command} の runner は 'direct' であるべき"
        assert source == "default"


def test_build_commandline_cargo_fmt_via_mise() -> None:
    """cargo-fmt の既定設定 (bin-runner=mise) で mise exec 形式のコマンドラインが組まれる。"""
    config = pyfltr.config.create_default_config()
    resolved = pyfltr.command.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["mise", "exec", "rust@latest", "--", "cargo"]
    assert resolved.runner == "bin-runner"
    assert resolved.effective_runner == "mise"


def test_build_commandline_cargo_fmt_runner_direct(mocker) -> None:
    """{command}-runner = "direct" を明示すると direct 経路で解決される。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/cargo")
    config = pyfltr.config.create_default_config()
    config.values["cargo-fmt-runner"] = "direct"
    resolved = pyfltr.command.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["/usr/local/bin/cargo"]
    assert resolved.effective_runner == "direct"
    assert resolved.runner_source == "explicit"


def test_build_commandline_dotnet_format_via_mise() -> None:
    """dotnet-format の既定設定で mise dotnet backend 形式になる。"""
    config = pyfltr.config.create_default_config()
    resolved = pyfltr.command.build_commandline("dotnet-format", config)
    assert resolved.commandline == ["mise", "exec", "dotnet@latest", "--", "dotnet"]


def test_build_commandline_explicit_mise_for_existing_bin_tool() -> None:
    """{command}-runner = "mise" 明示時もグローバル bin-runner と独立に動作する。"""
    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "direct"
    config.values["shellcheck-runner"] = "mise"
    config.values["shellcheck-version"] = "0.10.0"
    resolved = pyfltr.command.build_commandline("shellcheck", config)
    assert resolved.commandline == ["mise", "exec", "shellcheck@0.10.0", "--", "shellcheck"]
    assert resolved.effective_runner == "mise"


def test_build_commandline_mise_on_unregistered_tool_raises() -> None:
    """backend 未登録ツールに mise 明示するとエラー。"""
    config = pyfltr.config.create_default_config()
    config.values["typos-runner"] = "mise"
    with pytest.raises(ValueError, match="mise backend"):
        pyfltr.command.build_commandline("typos", config)


def test_build_commandline_js_runner_on_non_js_tool_raises() -> None:
    """js-runner 非対応ツールに js-runner 明示するとエラー。"""
    config = pyfltr.config.create_default_config()
    config.values["typos-runner"] = "js-runner"
    with pytest.raises(ValueError, match="js-runner"):
        pyfltr.command.build_commandline("typos", config)


def test_build_commandline_path_override_wins() -> None:
    """{command}-path が非空ならその値で direct 実行する（path-override）。"""
    config = pyfltr.config.create_default_config()
    config.values["cargo-fmt-path"] = "/opt/rust/bin/cargo"
    resolved = pyfltr.command.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["/opt/rust/bin/cargo"]
    assert resolved.runner_source == "path-override"
    assert resolved.effective_runner == "direct"


def test_build_commandline_dotnet_root_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """direct モードの dotnet 解決では DOTNET_ROOT 環境変数が PATH より優先される。"""
    candidate = tmp_path / "dotnet"
    candidate.write_text("#!/bin/sh\necho stub\n")
    candidate.chmod(0o755)
    monkeypatch.setenv("DOTNET_ROOT", str(tmp_path))

    config = pyfltr.config.create_default_config()
    config.values["dotnet-format-runner"] = "direct"
    resolved = pyfltr.command.build_commandline("dotnet-format", config)
    assert resolved.commandline == [str(candidate)]
    assert resolved.effective_runner == "direct"


def test_build_commandline_dotnet_root_ignored_in_mise_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """mise モードでは DOTNET_ROOT は参照されず、mise exec 形式のままとなる。"""
    candidate = tmp_path / "dotnet"
    candidate.write_text("#!/bin/sh\n")
    candidate.chmod(0o755)
    monkeypatch.setenv("DOTNET_ROOT", str(tmp_path))

    config = pyfltr.config.create_default_config()
    # 既定 bin-runner=mise → effective=mise
    resolved = pyfltr.command.build_commandline("dotnet-format", config)
    assert resolved.commandline[:2] == ["mise", "exec"]


def test_command_runner_validation_rejects_unknown_value(tmp_path: pathlib.Path) -> None:
    """{command}-runner に不正値を与えると load_config がエラーで弾く。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ntypos-runner = "bogus"\n')
    with pytest.raises(ValueError, match="typos-runner"):
        pyfltr.config.load_config(config_dir=tmp_path)
