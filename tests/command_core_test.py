"""command.pyのコアテスト。

dispatcher・共通処理・環境変数・コマンドライン解決・`run_subprocess`・
キャッシュ・only_failed・プロセス管理を検証する。
"""

# pylint: disable=protected-access  # _normalize_path_entry等の内部ヘルパー単体テスト経路
# pylint: disable=too-many-lines  # コアテストはfixture密結合化を避けるため分割しない方針
# pylint: disable=duplicate-code  # fake_run系のサブプロセスダブル定義が他テストファイルと類似

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

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.warnings_
from tests import conftest as _testconf


def test_build_subprocess_env_sets_supply_chain_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """サプライチェーン対策用の環境変数が既定値で注入される。"""
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    monkeypatch.delenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", raising=False)

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 day"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "1440"


def test_build_subprocess_env_sets_python_utf8_mode() -> None:
    """サブプロセスはPython UTF-8モードで動く。"""
    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["PYTHONUTF8"] == "1"


def test_build_subprocess_env_preserves_existing_supply_chain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ユーザーが既に環境変数を設定している場合は既存値を尊重する。"""
    monkeypatch.setenv("UV_EXCLUDE_NEWER", "1 week")
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "10080")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 week"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "10080"


def test_build_subprocess_env_via_mise_strips_mise_tool_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """`via_mise=True`のとき、PATHからmise toolパスが除外される。"""
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                "/home/u/.local/share/mise/installs/dotnet/10.0.0",
                "/home/u/.local/share/mise/dotnet-root",
                "/home/u/.local/share/mise/shims",
                "/home/u/.local/share/mise/bin",
                "/usr/bin",
            ]
        ),
    )

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "dotnet-build", via_mise=True)
    entries = env["PATH"].split(os.pathsep)

    assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" not in entries
    assert "/home/u/.local/share/mise/dotnet-root" not in entries
    assert "/home/u/.local/share/mise/shims" not in entries
    # mise本体バイナリディレクトリと無関係エントリは保持される
    assert "/home/u/.local/share/mise/bin" in entries
    assert "/usr/bin" in entries


def test_build_subprocess_env_default_keeps_mise_tool_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定（`via_mise=False`）ではmise toolパスを除外しない。"""
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(["/home/u/.local/share/mise/installs/dotnet/10.0.0", "/usr/bin"]),
    )

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "ruff-check")
    entries = env["PATH"].split(os.pathsep)

    assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" in entries
    assert "/usr/bin" in entries


def test_resolve_js_commandline_pnpx_with_textlint_packages() -> None:
    """pnpx runnerではtextlint-packagesが--packageで展開される。

    textlint本体のspecは`_JS_TOOL_PNPX_PACKAGE_SPEC`によって
    既知バグのあるバージョンを除外した形で指定される。
    """
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing", "textlint-rule-ja-no-abusage"]

    path, prefix = pyfltr.command.runner._resolve_js_commandline("textlint", config)

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
    """pnpx runnerの既定状態でもtextlint 15.5.3が除外specで指定される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("textlint", config)

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
    """markdownlintは除外対象外で、従来どおりbin名がそのまま渡される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("markdownlint", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == ["--package", "markdownlint-cli2", "markdownlint-cli2"]


def test_resolve_js_commandline_pnpm_ignores_packages() -> None:
    """pnpm runnerではtextlint-packagesは無視される（package.json側で管理前提）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    path, prefix = pyfltr.command.runner._resolve_js_commandline("textlint", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "textlint"]


def test_resolve_js_commandline_markdownlint_uses_cli2_binary() -> None:
    """markdownlintコマンドの実体はmarkdownlint-cli2。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("markdownlint", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "markdownlint-cli2"]


def test_resolve_js_commandline_pnpx_eslint() -> None:
    """pnpx runnerでeslintが通常通り（bin名 = パッケージ名）解決される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("eslint", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == ["--package", "eslint", "eslint"]


def test_resolve_js_commandline_pnpx_prettier() -> None:
    """pnpx runnerでprettierが通常通り解決される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("prettier", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    assert prefix == ["--package", "prettier", "prettier"]


def test_resolve_js_commandline_pnpx_biome_uses_scoped_package() -> None:
    """pnpx runnerでbiomeはスコープ付きパッケージ@biomejs/biomeで解決される。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("biome", config)

    assert pathlib.PurePath(path).stem == "pnpx"
    # --packageには@biomejs/biome、bin名はbiome
    assert prefix == ["--package", "@biomejs/biome", "biome"]


def test_resolve_js_commandline_pnpm_prettier() -> None:
    """pnpm runnerでprettierがpnpm exec prettierになる。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("prettier", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "prettier"]


def test_resolve_js_commandline_pnpm_biome() -> None:
    """pnpm runnerでbiomeがpnpm exec biomeになる（スコープ無効）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command.runner._resolve_js_commandline("biome", config)

    assert pathlib.PurePath(path).stem == "pnpm"
    assert prefix == ["exec", "biome"]


def test_resolve_js_commandline_npx() -> None:
    """npx runnerでは-pでパッケージを指定する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "npx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    path, prefix = pyfltr.command.runner._resolve_js_commandline("textlint", config)

    assert pathlib.PurePath(path).stem == "npx"
    assert prefix == [
        "--no-install",
        "-p",
        "textlint-rule-preset-ja-technical-writing",
        "--",
        "textlint",
    ]


def test_resolve_js_commandline_direct_missing_raises(tmp_path: pathlib.Path) -> None:
    """direct runnerでnode_modules/.bin/<cmd>が無ければFileNotFoundError。"""
    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "direct"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        with pytest.raises(FileNotFoundError):
            pyfltr.command.runner._resolve_js_commandline("textlint", config)
    finally:
        os.chdir(original_cwd)


def test_resolve_js_commandline_direct_found(tmp_path: pathlib.Path) -> None:
    """direct runnerでnode_modules/.bin/<cmd>があればpathを返す。"""
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "textlint").write_text("#!/bin/sh\necho stub\n")

    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "direct"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        path, prefix = pyfltr.command.runner._resolve_js_commandline("textlint", config)
        assert path.endswith("textlint")
        assert not prefix
    finally:
        os.chdir(original_cwd)


def test_execute_command_direct_missing_returns_failed_result(tmp_path: pathlib.Path) -> None:
    """js-runner=directで実行ファイル不在時、例外でなくresolution_failed CommandResultを返す。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    config = pyfltr.config.config.create_default_config()
    config.values["js-runner"] = "direct"
    config.values["textlint"] = True

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        result = pyfltr.command.dispatcher.execute_command(
            "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target])
        )
        assert result.status == "resolution_failed"
        assert result.has_error is True
        assert "node_modules" in result.output
    finally:
        os.chdir(original_cwd)


def test_run_subprocess_file_not_found_returns_127() -> None:
    """存在しない実行ファイルを指定しても例外を送出せずrc=127を返す。"""
    result = pyfltr.command.process.run_subprocess(
        ["this-command-definitely-does-not-exist-xyz-1234"],
        env={"PATH": "/nonexistent"},
    )
    assert result.returncode == 127
    assert "見つかりません" in result.stdout


def test_build_subprocess_env_npm_config_actually_effective(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """注入したNPM_CONFIG_MINIMUM_RELEASE_AGEが実際にnpm互換ツールに反映されることを確認する。

    環境変数名がtypoしたり、仕様変更で効かなくなったりした場合に検知する。
    既定値（1440）は実行環境のグローバル設定と区別できないため、
    ユーザー既定値優先（setdefault）の動作を利用して非標準値4321を注入し検証する。

    検証にはnpmを使用する。pnpmはインストール方法やバージョンにより
    NPM_CONFIG_*環境変数の読み取り動作が不安定なため（pnpm config getが
    env varを無視するケースがある）、npmのconfig getで代替する。
    npmはNPM_CONFIG_*規約の本家であり、動作が安定している。
    """
    # npmの設定ファイル読込を避けるため、隔離したHOMEを用意する。
    # XDG_CONFIG_HOMEも明示的に隔離してグローバル設定の干渉を排除する。
    original_home = pathlib.Path(os.environ.get("HOME") or os.environ["USERPROFILE"])
    mise_config = original_home / ".config" / "mise" / "config.toml"
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", str(mise_config))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    # 非標準値を設定し、build_subprocess_envがそのまま通すことを利用する。
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "4321")

    config = pyfltr.config.config.create_default_config()
    env = pyfltr.command.env.build_subprocess_env(config, "markdownlint")
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "4321"

    # Windowsではnpmがnpm.cmdとして提供されるため、shutil.whichで完全パスを取得する
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


def test_excluded_disabled_by_empty_config() -> None:
    """exclude/extend-excludeが空の場合、全パスが除外されないことを確認する（--no-exclude相当）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["exclude"] = []
    config.values["extend-exclude"] = []

    # 通常は除外されるパスが除外されないこと
    assert not pyfltr.command.targets.excluded(pathlib.Path(".venv"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path("node_modules"), config)
    assert not pyfltr.command.targets.excluded(pathlib.Path(".serena/memories/foo.md"), config)


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
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        config.values["extend-exclude"] = ["sample.py"]
        with caplog.at_level(logging.WARNING):
            result = pyfltr.command.targets.expand_all_files([target], config)
        assert len(result) == 0
        assert "除外設定により無視されました" in caplog.text
        assert 'extend-exclude="sample.py"' in caplog.text
    finally:
        os.chdir(original_cwd)


def test_expand_all_files_warns_missing_file(tmp_path: pathlib.Path, caplog) -> None:
    """直接指定されたパスが存在しない場合、警告が出て reason="missing" で蓄積される。"""
    # 絶対パス指定はcwd起点の相対パスへ変換されるため、cwd配下の相対パスとして検証する。
    target_name = "does_not_exist.py"
    target = pathlib.Path(target_name)

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
        assert pyfltr.warnings_.filtered_direct_files(reason="missing") == [target_name]
        assert not pyfltr.warnings_.filtered_direct_files(reason="excluded")
    finally:
        pyfltr.warnings_.clear()
        os.chdir(original_cwd)


def test_expand_all_files_warns_gitignored_file(tmp_path: pathlib.Path, caplog) -> None:
    """直接指定されたファイルが.gitignoreで除外された場合に警告が出る。"""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    target = tmp_path / "ignored.py"
    target.write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.config.create_default_config()
        with caplog.at_level(logging.WARNING):
            result = pyfltr.command.targets.expand_all_files([target], config)
        assert len(result) == 0
        assert ".gitignore により無視されました" in caplog.text
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


def test_build_auto_args_pylint_pydantic() -> None:
    """pylint-pydantic=trueの場合に自動引数が挿入される。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.runner._build_auto_args("pylint", config, [])
    assert "--load-plugins=pylint_pydantic" in result


def test_build_auto_args_mypy_unused_awaitable() -> None:
    """mypy-unused-awaitable=trueの場合に自動引数が挿入される。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.runner._build_auto_args("mypy", config, [])
    assert "--enable-error-code=unused-awaitable" in result


def test_build_auto_args_disabled() -> None:
    """自動オプションをfalseにすると引数が挿入されない。"""
    config = pyfltr.config.config.create_default_config()
    config.values["pylint-pydantic"] = False
    result = pyfltr.command.runner._build_auto_args("pylint", config, [])
    assert "--load-plugins=pylint_pydantic" not in result


def test_build_auto_args_dedup_with_user_args() -> None:
    """ユーザーが既に同じ引数を指定している場合はスキップする。"""
    config = pyfltr.config.config.create_default_config()
    user_args = ["--load-plugins=pylint_pydantic", "--jobs=4"]
    result = pyfltr.command.runner._build_auto_args("pylint", config, user_args)
    assert "--load-plugins=pylint_pydantic" not in result


def test_build_auto_args_no_match() -> None:
    """`AUTO_ARGS`に定義されていないコマンドは空リストを返す。"""
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.runner._build_auto_args("ruff-check", config, [])
    assert not result


def test_auto_args_included_in_commandline(mocker, tmp_path: pathlib.Path) -> None:
    """`execute_command`の結果コマンドラインに自動引数が含まれる。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["pylint"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["pylint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "pylint", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )
    assert "--load-plugins=pylint_pydantic" in result.commandline


# --- bin-runnerテスト ---


def test_resolve_bin_commandline_direct_found(mocker) -> None:
    """directモードでwhichが成功した場合、解決されたパスを返す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/shellcheck")

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"

    path, prefix = pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

    assert path == "/usr/local/bin/shellcheck"
    assert not prefix


def test_resolve_bin_commandline_direct_not_found(mocker) -> None:
    """directモードでwhichが失敗した場合、FileNotFoundErrorを送出する。"""
    mocker.patch("shutil.which", return_value=None)

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"

    with pytest.raises(FileNotFoundError, match="shellcheck"):
        pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)


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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["shellcheck-version"] = "0.9.0"

    path, prefix = pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = pyfltr.command.runner._resolve_bin_commandline("actionlint", config)

    assert path == "/usr/local/bin/actionlint"
    assert not prefix


def test_resolve_bin_commandline_mise_not_installed_no_fallback(mocker) -> None:
    """miseモードでmise未導入かつ対象バイナリもPATHに無い場合、FileNotFoundErrorを送出する。"""
    mocker.patch("shutil.which", return_value=None)

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError, match="actionlint"):
        pyfltr.command.runner._resolve_bin_commandline("actionlint", config)


def test_resolve_bin_commandline_glab_ci_lint_mise(mocker) -> None:
    """glab-ci-lintはmiseバックエンド経由でglabバイナリを解決する。

    `ci lint`サブコマンドはargs既定値側に持たせる設計のため、bin-runner解決の
    プレフィクスにはサブコマンドが含まれない（commandline組み立て段で付与される）。
    """
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = pyfltr.command.runner._resolve_bin_commandline("glab-ci-lint", config)

    assert path == "mise"
    assert prefix == ["exec", "glab@latest", "--", "glab"]
    # args既定値にサブコマンドが含まれていることを確認（明示path指定時にも有効化させるため）
    assert config["glab-ci-lint-args"] == ["ci", "lint"]


def test_resolve_bin_commandline_glab_ci_lint_direct(mocker) -> None:
    """directモードではPATH上のglabバイナリを解決する。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/glab")

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"

    path, prefix = pyfltr.command.runner._resolve_bin_commandline("glab-ci-lint", config)

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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError, match="tool not found"):
        pyfltr.command.runner._resolve_bin_commandline("ec", config)


def test_resolve_bin_commandline_mise_untrusted_auto_trust_success(mocker) -> None:
    """未信頼エラー→trust成功→再チェック成功の3段で最終的に通常成功扱いになる。"""
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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    path, prefix = pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

    assert path == "mise"
    assert prefix == ["exec", "shellcheck@latest", "--", "shellcheck"]
    # 事前チェック→trust→リトライの3回が実際に発生したことを確認
    assert mock_run.call_count == 3


def test_resolve_bin_commandline_mise_untrusted_auto_trust_disabled(mocker) -> None:
    """mise-auto-trust=Falseのときtrustが呼ばれず、stderr含むエラーメッセージで失敗する。"""
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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = False

    with pytest.raises(FileNotFoundError, match="not trusted"):
        pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

    # trustコマンドは呼ばれていないことを確認（subprocess.runの呼び出しは1回のみ）
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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="plugin not found"):
        pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

    # trustコマンドは呼ばれていないことを確認（subprocess.runの呼び出しは1回のみ）
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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="some other failure after trust"):
        pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)


def test_resolve_bin_commandline_mise_untrusted_auto_trust_trust_failure(mocker) -> None:
    """mise trustコマンド自体が失敗した場合、trust.stderrを含むエラーで即座に失敗する。"""
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

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True

    with pytest.raises(FileNotFoundError, match="permission denied"):
        pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)


def test_ensure_mise_available_passes_stripped_env_to_subprocess(mocker, monkeypatch) -> None:
    """`mise exec --version`呼び出し時にPATHからmise toolパスが除外されたenvが渡る。

    miseが親PATHに自身のtoolエントリを見つけるとtools解決をスキップしてPATH解決へ
    フォールバックする挙動を回避するためのガード。
    """
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                "/home/u/.local/share/mise/installs/dotnet/10.0.0",
                "/home/u/.local/share/mise/dotnet-root",
                "/home/u/.local/share/mise/shims",
                "/home/u/.local/share/mise/bin",
                "/usr/bin",
            ]
        ),
    )
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

    assert mock_run.call_count == 1
    passed_env = mock_run.call_args.kwargs["env"]
    entries = passed_env["PATH"].split(os.pathsep)
    # mise toolパスは除外、mise/binと通常エントリは保持される
    assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" not in entries
    assert "/home/u/.local/share/mise/dotnet-root" not in entries
    assert "/home/u/.local/share/mise/shims" not in entries
    assert "/home/u/.local/share/mise/bin" in entries
    assert "/usr/bin" in entries


def test_ensure_mise_available_resolution_failure_includes_direct_hint(mocker) -> None:
    """`mise exec`解決失敗時のエラー文面に`{command}-runner = "direct"`への切替案内が含まれる。

    mise registryからツールが消失した場合などにユーザーが回避策へ自力で辿り着けるよう、
    エラー文面でdirect経路への切替案内を提示する。
    """
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["mise", "exec"],
            returncode=1,
            stdout="",
            stderr="mise ERROR plugin not found: cargo-deny",
        ),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError) as excinfo:
        pyfltr.command.runner._resolve_bin_commandline("cargo-deny", config)

    message = str(excinfo.value)
    # 既存のmise由来情報（ERROR内容）は引き続き含まれる。
    assert "plugin not found" in message
    # 回避策の案内が一文として含まれる。
    assert 'cargo-deny-runner = "direct"' in message


def test_ensure_mise_available_passes_stripped_env_to_trust(mocker, monkeypatch) -> None:
    """`mise trust`呼び出し時にもPATHからmise toolパスが除外されたenvが渡る。"""
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            [
                "/home/u/.local/share/mise/installs/dotnet/10.0.0",
                "/home/u/.local/share/mise/shims",
                "/usr/bin",
            ]
        ),
    )
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        side_effect=[
            # 事前チェック: 未信頼で失敗
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
            # 再チェック: 成功
            subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
        ],
    )

    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "mise"
    config.values["mise-auto-trust"] = True
    pyfltr.command.runner._resolve_bin_commandline("shellcheck", config)

    # 3回すべてのsubprocess.run呼び出しにmise toolパス除外済みenvが渡る
    assert mock_run.call_count == 3
    for call in mock_run.call_args_list:
        passed_env = call.kwargs["env"]
        entries = passed_env["PATH"].split(os.pathsep)
        assert "/home/u/.local/share/mise/installs/dotnet/10.0.0" not in entries
        assert "/home/u/.local/share/mise/shims" not in entries
        assert "/usr/bin" in entries


def test_failed_resolution_result() -> None:
    """`_failed_resolution_result`が解決失敗専用のCommandResultを返す。"""
    command_info = pyfltr.config.config.CommandInfo(type="linter")

    result = pyfltr.command.dispatcher._failed_resolution_result(
        "shellcheck", command_info, "ツールが見つかりません: shellcheck", files=3
    )

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
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["tsc"] = True
    # tscはデフォルトでpass-filenames=false
    assert config["tsc-pass-filenames"] is False

    result = pyfltr.command.dispatcher.execute_command(
        "tsc", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

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
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "ruff-check", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    # ファイルパスがコマンドラインに含まれることを確認
    assert str(target) in cmdline
    assert result.status == "succeeded"


def test_bin_tool_spec_all_tools_defined() -> None:
    """`_BIN_TOOL_SPEC`に全bin系ツールが定義されている。"""
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
        # cargo系・dotnet系もbin-runner経路へ統合済み（mise backend経由で解決）。
        "cargo-fmt",
        "cargo-clippy",
        "cargo-check",
        "cargo-test",
        "cargo-deny",
        "dotnet-format",
        "dotnet-build",
        "dotnet-test",
    }
    assert set(pyfltr.command.runner._BIN_TOOL_SPEC.keys()) == expected_tools


def test_bin_tool_spec_structure() -> None:
    """`BinToolSpec`のフィールドが正しく設定されている。"""
    spec = pyfltr.command.runner._BIN_TOOL_SPEC["ec"]
    assert spec.bin_name == "ec"
    assert spec.mise_backend == "editorconfig-checker"
    assert spec.default_version == "latest"

    spec = pyfltr.command.runner._BIN_TOOL_SPEC["shellcheck"]
    assert spec.bin_name == "shellcheck"

    # cargo-denyはmise registryから消失したため、aquaレジストリ経由を既定とする。
    spec = pyfltr.command.runner._BIN_TOOL_SPEC["cargo-deny"]
    assert spec.bin_name == "cargo-deny"
    assert spec.mise_backend == "aqua:EmbarkStudios/cargo-deny"


def test_command_result_cached_defaults() -> None:
    """`CommandResult`の新フィールドcached/cached_fromの既定値テスト。"""
    result = pyfltr.command.core_.CommandResult(
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
    """キャッシュヒット時はsubprocess実行をスキップしてcached=Trueを返す。"""
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    cache_root = tmp_path / ".cache"
    store = pyfltr.state.cache.CacheStore(cache_root=cache_root)

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess")

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-path"] = "/bin/true"  # js-runnerを使わずpath指定で解決を単純化

    # 1回目: キャッシュミスでsubprocess実行
    mock_run.return_value = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="ok")
    result1 = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01ABCDEFGH"),
    )
    assert mock_run.call_count == 1
    assert result1.cached is False

    # 2回目: キャッシュヒットでsubprocess実行されない
    result2 = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01XYZ"),
    )
    assert mock_run.call_count == 1  # 増えていない
    assert result2.cached is True
    assert result2.cached_from == "01ABCDEFGH"


def test_execute_command_non_cacheable_skips_cache(mocker, tmp_path: pathlib.Path) -> None:
    """cacheable=Falseのツール（mypy等）はキャッシュに書かれない。"""
    target = tmp_path / "foo.py"
    target.write_text("x = 1\n")
    cache_root = tmp_path / ".cache"
    store = pyfltr.state.cache.CacheStore(cache_root=cache_root)

    mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess(["mypy"], returncode=0, stdout=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True

    pyfltr.command.dispatcher.execute_command(
        "mypy",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01ABCDEFGH"),
    )
    # mypyはcacheable=Falseのため、キャッシュエントリは作られない
    assert not list(cache_root.rglob("*.json"))


def test_execute_command_only_failed_targets_files_override(mocker, tmp_path: pathlib.Path) -> None:
    """`only_failed_targets`にToolTargets.with_filesを渡すと`all_files`の代わりにその集合が対象になる。"""
    file_a = tmp_path / "a.py"
    file_b = tmp_path / "b.py"
    file_a.write_text("x = 1\n")
    file_b.write_text("y = 2\n")

    mock_run = mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess(["ruff"], returncode=0, stdout=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True

    result = pyfltr.command.dispatcher.execute_command(
        "ruff-check",
        _testconf.make_args(),
        _testconf.make_execution_context(
            config, [file_a, file_b], only_failed_targets=pyfltr.state.only_failed.ToolTargets.with_files([file_b])
        ),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_b) in cmdline
    assert str(file_a) not in cmdline
    # CommandResult.target_filesもToolTargetsベースに絞られる
    assert result.target_files == [file_b]


def test_execute_command_only_failed_targets_fallback_uses_all_files(mocker, tmp_path: pathlib.Path) -> None:
    """`ToolTargets.fallback_default()`なら既定の`all_files`で実行される。"""
    file_a = tmp_path / "a.py"
    file_a.write_text("x = 1\n")

    mock_run = mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess(["ruff"], returncode=0, stdout=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True

    pyfltr.command.dispatcher.execute_command(
        "ruff-check",
        _testconf.make_args(),
        _testconf.make_execution_context(
            config, [file_a], only_failed_targets=pyfltr.state.only_failed.ToolTargets.fallback_default()
        ),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_a) in cmdline


def test_execute_command_only_failed_targets_none_uses_default(mocker, tmp_path: pathlib.Path) -> None:
    """`only_failed_targets=None`なら既定の`all_files`で実行される（--only-failed未指定）。"""
    file_a = tmp_path / "a.py"
    file_a.write_text("x = 1\n")

    mock_run = mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess(["ruff"], returncode=0, stdout=""),
    )

    config = pyfltr.config.config.create_default_config()
    config.values["ruff-check"] = True

    pyfltr.command.dispatcher.execute_command(
        "ruff-check",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [file_a], only_failed_targets=None),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_a) in cmdline


def test_pick_targets_none_when_targets_is_none() -> None:
    """`only_failed_targets=None`のとき、コマンドに関係なくNoneを返す。"""
    result = pyfltr.command.targets.pick_targets(None, "ruff-check")
    assert result is None


def test_pick_targets_returns_entry_for_matching_command(tmp_path: pathlib.Path) -> None:
    """`only_failed_targets`dictにコマンドが含まれるとき、対応するToolTargetsを返す。"""
    file_a = tmp_path / "a.py"
    targets = {"ruff-check": pyfltr.state.only_failed.ToolTargets.with_files([file_a])}
    result = pyfltr.command.targets.pick_targets(targets, "ruff-check")
    assert result is not None
    assert result.mode == "files"
    assert result.files == (file_a,)


def test_pick_targets_returns_none_for_missing_command() -> None:
    """`only_failed_targets`dictにコマンドが含まれないときNoneを返す。"""
    targets: dict[str, pyfltr.state.only_failed.ToolTargets] = {}
    result = pyfltr.command.targets.pick_targets(targets, "mypy")
    assert result is None


class _FakePopen:
    """`subprocess.Popen`を差し替えるための最小スタブ。

    `run_subprocess`のテスト用。Popenのwith文経由での利用とstdout逐次読み込み・
    wait()までを満たす最小限の振る舞いを提供する。起動引数はクラス変数
    `last_args_holder`のリスト内に追記する（None判定を避けてpylintの型縮めに頼らない）。
    """

    last_args_holder: list[list[str]] = []

    def __init__(self, args: list[str], **kwargs: typing.Any) -> None:
        del kwargs  # Popen互換の追加引数を受け取るのみ
        _FakePopen.last_args_holder.append(list(args))
        self.returncode = 0
        self.stdout: typing.Iterator[str] = iter([])

    def __enter__(self) -> "_FakePopen":
        """with文のエントリー。"""
        return self

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        """with文のイグジット。"""
        del exc_type, exc, tb  # contextmanager互換の引数を受け取るのみ

    def wait(self) -> int:
        """プロセス終了待ち。ダミーで直ちにreturncodeを返す。"""
        return self.returncode


def test_run_subprocess_resolves_command_via_shutil_which(mocker) -> None:
    """`commandline[0]`が`shutil.which`で解決されてPopenに渡る。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.shutil.which", return_value="/resolved/pre-commit")
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess(["pre-commit", "run", "--all-files"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [["/resolved/pre-commit", "run", "--all-files"]]


def test_run_subprocess_keeps_original_name_when_unresolved(mocker) -> None:
    """`shutil.which`がNoneなら元のコマンド名のままPopenに渡る。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.shutil.which", return_value=None)
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess(["missing-tool", "arg"], {"PATH": "/usr/bin"})

    assert _FakePopen.last_args_holder == [["missing-tool", "arg"]]


def test_run_subprocess_resolves_via_env_path(mocker, tmp_path: pathlib.Path, monkeypatch) -> None:
    """`os.environ["PATH"]`では見えず`env["PATH"]`にだけある実行ファイルが解決される。

    解決探索対象PATHとPopenへ渡す`env["PATH"]`の一致要件に対するリグレッション防止。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # WindowsのshUtil.whichはPATHEXTに列挙された拡張子で実行ファイルを判定するため、
    # ダミー実行ファイル名を`.bat`にする（POSIXでは実行属性0o755で判定される）。
    # 本テストの主眼はenv["PATH"]経由での解決可否であり、拡張子/実行属性の違いは付随的。
    if os.name == "nt":
        target = bin_dir / "faketool.bat"
        target.write_text("")
    else:
        target = bin_dir / "faketool"
        target.write_text("")
        target.chmod(0o755)

    # os.environのPATHからはbin_dirを除外する（env["PATH"]経由で解決することの検証）
    monkeypatch.setenv("PATH", "/nonexistent-pyfltr-test-path")

    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    pyfltr.command.process.run_subprocess(["faketool"], {"PATH": str(bin_dir)})

    # 解決されたパスが渡ること（先頭要素が/tmp/.../bin/faketool*を指す）
    assert len(_FakePopen.last_args_holder) == 1
    resolved = pathlib.Path(_FakePopen.last_args_holder[0][0])
    assert resolved.name.startswith("faketool")
    assert resolved.parent == bin_dir


def test_run_subprocess_does_not_mutate_commandline(mocker) -> None:
    """呼び出し側の`commandline`リストは書き換えない（retry_command等に影響するため）。"""
    _FakePopen.last_args_holder = []
    mocker.patch("pyfltr.command.process.shutil.which", return_value="/resolved/tool")
    mocker.patch("pyfltr.command.process.subprocess.Popen", _FakePopen)

    original = ["tool", "arg"]
    pyfltr.command.process.run_subprocess(original, {"PATH": "/usr/bin"})

    assert original == ["tool", "arg"]


def test_get_env_path_windows_uses_case_insensitive_key(monkeypatch) -> None:
    """Windows（`os.name == "nt"`）では`Path`キーも`PATH`として採用される。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin"}) == "/tmp/bin"
    assert pyfltr.command.env.get_env_path({"path": "/tmp/bin"}) == "/tmp/bin"
    # PATH大文字が存在する場合も取れる
    assert pyfltr.command.env.get_env_path({"PATH": "/tmp/bin"}) == "/tmp/bin"


def test_get_env_path_posix_strict_key(monkeypatch) -> None:
    """POSIXでは`env.get("PATH")`のみを使い、`Path`キーは採用しない。

    `env={"Path": "/tmp/bin", "PATH": "/usr/bin"}`で解決側とPopen実行時側のPATHが
    不一致になる事故を防ぐ設計。
    """
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin"}) is None
    assert pyfltr.command.env.get_env_path({"PATH": "/usr/bin"}) == "/usr/bin"
    # 両方あってもPATHのみを採用する
    assert pyfltr.command.env.get_env_path({"Path": "/tmp/bin", "PATH": "/usr/bin"}) == "/usr/bin"


def test_normalize_path_entry_for_dedup_posix(monkeypatch) -> None:
    """POSIXでは末尾スラッシュのみ落とし、大文字小文字は保持する。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    assert pyfltr.command.env._normalize_path_entry_for_dedup("/usr/bin/") == "/usr/bin"
    assert pyfltr.command.env._normalize_path_entry_for_dedup("/USR/Bin") == "/USR/Bin"
    assert pyfltr.command.env._normalize_path_entry_for_dedup("") == ""


def test_normalize_path_entry_for_dedup_windows(monkeypatch) -> None:
    """Windowsでは大文字小文字非区別 + パス区切り正規化。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    assert pyfltr.command.env._normalize_path_entry_for_dedup("C:/Tools/Mise/bin") == "c:\\tools\\mise\\bin"
    assert pyfltr.command.env._normalize_path_entry_for_dedup("c:\\tools\\mise\\bin\\") == "c:\\tools\\mise\\bin"
    assert pyfltr.command.env._normalize_path_entry_for_dedup("") == ""


def test_dedupe_path_value_preserves_first_occurrence(monkeypatch) -> None:
    """重複は順序先勝ちで除去され、初出エントリの表記が保持される。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    src = ":".join(["/usr/bin", "/opt/bin", "/usr/bin", "/usr/local/bin", "/opt/bin/"])
    assert pyfltr.command.env._dedupe_path_value(src) == ":".join(["/usr/bin", "/opt/bin", "/usr/local/bin"])


def test_dedupe_path_value_windows_case_insensitive(monkeypatch) -> None:
    """Windowsでは大文字小文字差・パス区切り差を吸収して重複扱いする。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    src = ";".join(["C:\\Tools\\Mise\\bin", "c:/tools/mise/bin", "C:\\Windows"])
    deduped = pyfltr.command.env._dedupe_path_value(src)
    assert deduped == ";".join(["C:\\Tools\\Mise\\bin", "C:\\Windows"])


def test_dedupe_path_value_keeps_empty_entry_only_once(monkeypatch) -> None:
    """空エントリ（POSIXでcwd相当）も最初の1回のみ残す。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    assert pyfltr.command.env._dedupe_path_value("/usr/bin::/opt/bin:") == ":".join(["/usr/bin", "", "/opt/bin"])


def test_dedupe_environ_path_writes_back_with_same_key(monkeypatch) -> None:
    """書き戻しは検出したPATHキー名を保持する（`Path` / `PATH`揺れ対応）。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ";")
    env: dict[str, str] = {"Path": ";".join(["c:/tools", "C:/Tools"])}
    assert pyfltr.command.env.dedupe_environ_path(env) is True
    assert "Path" in env
    assert "PATH" not in env
    assert env["Path"] == "c:/tools"


def test_dedupe_environ_path_no_change_when_unique(monkeypatch) -> None:
    """重複が無ければ書き換え不要として`False`を返す。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    env: dict[str, str] = {"PATH": "/usr/bin:/opt/bin"}
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert env["PATH"] == "/usr/bin:/opt/bin"


def test_dedupe_environ_path_returns_false_when_path_missing() -> None:
    """PATH未設定なら何もせず`False`。"""
    env: dict[str, str] = {}
    assert pyfltr.command.env.dedupe_environ_path(env) is False
    assert not env


def test_is_mise_tool_path_marker_matches(monkeypatch) -> None:
    """mise toolパスのマーカーが含まれるエントリは`True`。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    assert pyfltr.command.env._is_mise_tool_path("/home/u/.local/share/mise/installs/dotnet/10.0.0") is True
    assert pyfltr.command.env._is_mise_tool_path("/home/u/.local/share/mise/dotnet-root") is True
    assert pyfltr.command.env._is_mise_tool_path("/home/u/.local/share/mise/shims") is True


def test_is_mise_tool_path_protects_mise_bin(monkeypatch) -> None:
    """mise/binは保護対象（mise本体バイナリディレクトリのため）。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    assert pyfltr.command.env._is_mise_tool_path("/home/u/.local/share/mise/bin") is False


def test_is_mise_tool_path_unrelated_returns_false(monkeypatch) -> None:
    """miseと関係ないパスや空エントリは`False`。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    assert pyfltr.command.env._is_mise_tool_path("/usr/bin") is False
    assert pyfltr.command.env._is_mise_tool_path("") is False


def test_is_mise_tool_path_windows_case_and_separator(monkeypatch) -> None:
    """Windowsでは大文字混在・`\\`区切りでも判定できる。mise/binは保護対象。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "nt")
    assert pyfltr.command.env._is_mise_tool_path("C:\\Users\\u\\AppData\\Local\\MISE\\Installs\\Dotnet\\10.0") is True
    assert pyfltr.command.env._is_mise_tool_path("C:\\Users\\u\\AppData\\Local\\mise\\dotnet-root") is True
    assert pyfltr.command.env._is_mise_tool_path("C:\\Users\\u\\AppData\\Local\\mise\\shims") is True
    assert pyfltr.command.env._is_mise_tool_path("C:\\Users\\u\\AppData\\Local\\mise\\bin") is False


def test_build_mise_subprocess_env_does_not_mutate_input(monkeypatch) -> None:
    """`build_mise_subprocess_env`は入力envを破壊しない（純関数）。"""
    monkeypatch.setattr("pyfltr.command.env.os.name", "posix")
    monkeypatch.setattr("pyfltr.command.env.os.pathsep", ":")
    src: dict[str, str] = {"PATH": "/home/u/.local/share/mise/installs/dotnet/10.0:/usr/bin"}
    new = pyfltr.command.env.build_mise_subprocess_env(src)

    # 入力辞書は変更されない
    assert src == {"PATH": "/home/u/.local/share/mise/installs/dotnet/10.0:/usr/bin"}
    # 戻り値はmise toolパスを除外したPATHを持つ
    assert new["PATH"] == "/usr/bin"


def test_build_mise_subprocess_env_handles_missing_path() -> None:
    """PATH未設定時は単にコピーを返す。"""
    src: dict[str, str] = {"FOO": "bar"}
    new = pyfltr.command.env.build_mise_subprocess_env(src)
    assert new == src
    assert new is not src


def _spawn_parent_with_child(script: str) -> tuple[subprocess.Popen[str], int, int]:
    """Pythonスクリプトをsubprocessとして起動し親pidと子pidを取得する。

    スクリプトは最初の1行に自身と子のpidを空白区切りでprintする契約。
    Popenは`start_new_session=True`で起動する（本番と同じ条件）。
    """
    # pylint: disable=consider-using-with
    # テスト対象の`active_processes`へ外から登録するため、`with`構文では
    # スコープ外でprocを扱えない。各テストのfinallyで解放する。
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
    """`pids`が全て消滅するまで最大`timeout`秒待つ。残存するpidを返す。

    initを持たないコンテナー環境では親reapが行われずzombieが残存するため、
    zombie状態は消滅扱いとする（プロセスツリーは既に停止しており、
    `terminate_active_processes`の責務は果たされている）。
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
    """`terminate_active_processes`が孫プロセスまで確実に停止する。

    Popen子が更にサブプロセスをforkするpytest-xdist相当の構造で、
    `start_new_session=True`相当のpgid分離によりSIGTERMが全体へ届くことを検証する。
    """
    script = textwrap.dedent(
        """
        import os, time
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            # child: pidをpipeへ書き、あとは待機する（stdoutへは書かない）。
            os.close(r)
            os.write(w, str(os.getpid()).encode())
            os.close(w)
            while True:
                time.sleep(1)
        else:
            # 親: childのpidを読み取り、自身とchildのpidを1行にまとめて出力する。
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
        with pyfltr.command.process.active_processes_lock:
            pyfltr.command.process.active_processes.append(proc)
        assert psutil.pid_exists(parent_pid)
        assert psutil.pid_exists(child_pid)

        pyfltr.command.process.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([parent_pid, child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかったpid: {remaining}"
    finally:
        with pyfltr.command.process.active_processes_lock:
            if proc in pyfltr.command.process.active_processes:
                pyfltr.command.process.active_processes.remove(proc)
        if proc.poll() is None:
            # POSIX限定パスのクリーンアップ。Windowsではskipifで到達しない。
            # 型チェッカー（pyright / ty）のattr-defined誤検知は局所コメントで抑止する。
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(os.getpgid(proc.pid), 9)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore  # pylint: disable=no-member
        proc.wait(timeout=2.0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX 前提の killpg 経路を検証する")
def test_terminate_active_processes_parent_exited_grandchild_remains() -> None:
    """親が先にexitして孫だけがstdoutを握り残す構成でも停止できる。

    `start_new_session=True`によりpgidがproc.pidと一致するため、
    親reap後でも`os.killpg(proc.pid, SIGTERM)`で孫へ届くことを検証する。
    """
    script = textwrap.dedent(
        """
        import os, time
        pid = os.fork()
        if pid == 0:
            # grandchild役。stdoutを継承したまま待機する。
            while True:
                time.sleep(1)
        else:
            # 親だけがstdoutに書き出してすぐexit。grandchildはstdoutを握り続ける。
            print(f"{os.getpid()} {pid}", flush=True)
            os._exit(0)
        """
    )
    proc, _parent_pid, child_pid = _spawn_parent_with_child(script)
    try:
        with pyfltr.command.process.active_processes_lock:
            pyfltr.command.process.active_processes.append(proc)
        # 親は速やかにexitする。孫（子）は生存継続。
        proc.wait(timeout=2.0)
        assert psutil.pid_exists(child_pid), "孫プロセスが消えている"

        pyfltr.command.process.terminate_active_processes(timeout=3.0)

        remaining = _wait_gone([child_pid], timeout=3.0)
        assert remaining == [], f"停止できなかったpid: {remaining}"
    finally:
        with pyfltr.command.process.active_processes_lock:
            if proc in pyfltr.command.process.active_processes:
                pyfltr.command.process.active_processes.remove(proc)
        if psutil.pid_exists(child_pid):
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.kill(child_pid, 9)


def test_looks_like_glab_host_missing_detects_known_patterns() -> None:
    """ホスト未検出/未認証文言が大小文字差や前後文字を含んでも検出される。"""
    assert pyfltr.command.glab._looks_like_glab_host_missing(
        "Error: none of the git remotes configured for this repository point to a known GitLab host."
    )
    assert pyfltr.command.glab._looks_like_glab_host_missing("you are NOT AUTHENTICATED to glab")
    assert not pyfltr.command.glab._looks_like_glab_host_missing(
        "Error: validation failed: jobs:test config key may not be used with `rules`"
    )
    assert not pyfltr.command.glab._looks_like_glab_host_missing("")


def _make_glab_ci_lint_args() -> argparse.Namespace:
    """`execute_glab_ci_lint`で参照される最低限の属性を持つNamespaceを返す。"""
    return argparse.Namespace(verbose=False)


def _make_glab_ci_lint_command_info() -> pyfltr.config.config.CommandInfo:
    return pyfltr.config.config.BUILTIN_COMMANDS["glab-ci-lint"]


def test_execute_glab_ci_lint_skips_on_host_missing(mocker, tmp_path: pathlib.Path) -> None:
    """ホスト未検出stderrを検出したらreturncode=Noneでスキップ扱いに書き換える。"""
    pyfltr.warnings_.clear()
    proc = subprocess.CompletedProcess(
        args=["glab", "ci", "lint"],
        returncode=1,
        stdout="Error: none of the git remotes configured for this repository point to a known GitLab host.\n",
    )
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")

    result = pyfltr.command.glab.execute_glab_ci_lint(
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
    """ホスト未検出以外の非ゼロ終了はfailedのまま据え置く。"""
    pyfltr.warnings_.clear()
    proc = subprocess.CompletedProcess(
        args=["glab", "ci", "lint"],
        returncode=1,
        stdout="Error: validation failed: jobs:test config key may not be used with `rules`\n",
    )
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")

    result = pyfltr.command.glab.execute_glab_ci_lint(
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
    """正常終了はそのままsucceededとして扱い、ロケール強制環境変数を渡す。"""
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

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture)

    result = pyfltr.command.glab.execute_glab_ci_lint(
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


# --- {command}-runner per-tool解決のテスト ---


def test_resolve_runner_default_for_existing_bin_tools() -> None:
    """既存のbin-runner対応8ツールおよびcargo / dotnet系の{command}-runner既定値は"bin-runner"。"""
    config = pyfltr.config.config.create_default_config()
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
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "bin-runner", f"{command}のrunnerは'bin-runner'であるべき"
        assert source == "default"


def test_resolve_runner_default_for_js_tools() -> None:
    """JS系ツール（eslint / prettier / biome / oxlint / tsc / vitest / markdownlint / textlint）の既定は"js-runner"。"""
    config = pyfltr.config.config.create_default_config()
    for command in ("eslint", "prettier", "biome", "oxlint", "tsc", "vitest", "markdownlint", "textlint"):
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "js-runner", f"{command}のrunnerは'js-runner'であるべき"
        assert source == "default"


def test_resolve_runner_default_for_direct_tools() -> None:
    """typos / yamllint / Python系ツールの既定は"direct"。"""
    config = pyfltr.config.config.create_default_config()
    for command in ("typos", "yamllint", "mypy", "pylint", "pyright", "ty", "ruff-check", "ruff-format", "pytest", "uv-sort"):
        runner, source = pyfltr.command.runner.resolve_runner(command, config)
        assert runner == "direct", f"{command}のrunnerは'direct'であるべき"
        assert source == "default"


def test_build_commandline_cargo_fmt_via_mise() -> None:
    """cargo-fmtの既定設定（bin-runner=mise）でmise exec形式のコマンドラインが組まれる。"""
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["mise", "exec", "rust@latest", "--", "cargo"]
    assert resolved.runner == "bin-runner"
    assert resolved.effective_runner == "mise"


def test_build_commandline_cargo_fmt_runner_direct(mocker) -> None:
    """`{command}-runner = "direct"`を明示するとdirect経路で解決される。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/cargo")
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt-runner"] = "direct"
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["/usr/local/bin/cargo"]
    assert resolved.effective_runner == "direct"
    assert resolved.runner_source == "explicit"


def test_build_commandline_dotnet_format_via_mise() -> None:
    """dotnet-formatの既定設定でmise dotnet backend形式になる。"""
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("dotnet-format", config)
    assert resolved.commandline == ["mise", "exec", "dotnet@latest", "--", "dotnet"]


def test_build_commandline_cargo_deny_via_mise_uses_aqua_backend() -> None:
    """cargo-denyの既定設定でaqua backend経由のtool specが組まれる。

    mise registryからcargo-denyが消失したため、本家aquaレジストリ経由の
    `aqua:EmbarkStudios/cargo-deny`を既定backendとして採用する。
    """
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    assert resolved.commandline == ["mise", "exec", "aqua:EmbarkStudios/cargo-deny@latest", "--", "cargo-deny"]


def test_build_commandline_version_with_at_sign_used_as_full_tool_spec() -> None:
    """`{command}-version`値が`@`を含むときtool spec全体として扱われる。

    既定backendを上書きしたい利用者向けの拡張。例えばcargo-deny-versionに
    `cargo-deny@latest`を与えると、mise registry経由の旧挙動を再現できる。
    """
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-deny-version"] = "cargo-deny@latest"
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    # 既定backend（aqua:EmbarkStudios/cargo-deny）は適用されず、valueがそのまま渡る。
    assert resolved.commandline == ["mise", "exec", "cargo-deny@latest", "--", "cargo-deny"]


def test_build_commandline_version_with_colon_used_as_full_tool_spec() -> None:
    """`{command}-version`値が`:`を含むとき任意backend指定として扱われる。"""
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-deny-version"] = "aqua:EmbarkStudios/cargo-deny@0.16.0"
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    assert resolved.commandline == ["mise", "exec", "aqua:EmbarkStudios/cargo-deny@0.16.0", "--", "cargo-deny"]


def test_build_commandline_version_simple_keeps_legacy_format() -> None:
    """単純バージョン文字列の場合は従来通り`<tool>@<version>`で組み立てられる。"""
    config = pyfltr.config.config.create_default_config()
    config.values["shellcheck-version"] = "0.10.0"
    resolved = pyfltr.command.runner.build_commandline("shellcheck", config)
    assert resolved.commandline == ["mise", "exec", "shellcheck@0.10.0", "--", "shellcheck"]


def test_build_commandline_explicit_mise_for_existing_bin_tool() -> None:
    """`{command}-runner = "mise"`明示時もグローバルbin-runnerと独立に動作する。"""
    config = pyfltr.config.config.create_default_config()
    config.values["bin-runner"] = "direct"
    config.values["shellcheck-runner"] = "mise"
    config.values["shellcheck-version"] = "0.10.0"
    resolved = pyfltr.command.runner.build_commandline("shellcheck", config)
    assert resolved.commandline == ["mise", "exec", "shellcheck@0.10.0", "--", "shellcheck"]
    assert resolved.effective_runner == "mise"


def test_build_commandline_mise_on_unregistered_tool_raises() -> None:
    """backend未登録ツールにmise明示するとエラー。"""
    config = pyfltr.config.config.create_default_config()
    config.values["typos-runner"] = "mise"
    with pytest.raises(ValueError, match="mise backend"):
        pyfltr.command.runner.build_commandline("typos", config)


def test_build_commandline_js_runner_on_non_js_tool_raises() -> None:
    """js-runner非対応ツールにjs-runner明示するとエラー。"""
    config = pyfltr.config.config.create_default_config()
    config.values["typos-runner"] = "js-runner"
    with pytest.raises(ValueError, match="js-runner"):
        pyfltr.command.runner.build_commandline("typos", config)


def test_build_commandline_path_override_wins() -> None:
    """`{command}-path`が非空ならその値でdirect実行する（path-override）。"""
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt-path"] = "/opt/rust/bin/cargo"
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["/opt/rust/bin/cargo"]
    assert resolved.runner_source == "path-override"
    assert resolved.effective_runner == "direct"


def test_build_commandline_dotnet_root_priority(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """directモードのdotnet解決ではDOTNET_ROOT環境変数がPATHより優先される。"""
    candidate = tmp_path / "dotnet"
    candidate.write_text("#!/bin/sh\necho stub\n")
    candidate.chmod(0o755)
    monkeypatch.setenv("DOTNET_ROOT", str(tmp_path))

    config = pyfltr.config.config.create_default_config()
    config.values["dotnet-format-runner"] = "direct"
    resolved = pyfltr.command.runner.build_commandline("dotnet-format", config)
    assert resolved.commandline == [str(candidate)]
    assert resolved.effective_runner == "direct"


def test_build_commandline_dotnet_root_ignored_in_mise_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """miseモードではDOTNET_ROOTは参照されず、mise exec形式のままとなる。"""
    candidate = tmp_path / "dotnet"
    candidate.write_text("#!/bin/sh\n")
    candidate.chmod(0o755)
    monkeypatch.setenv("DOTNET_ROOT", str(tmp_path))

    config = pyfltr.config.config.create_default_config()
    # 既定bin-runner=mise → effective=mise
    resolved = pyfltr.command.runner.build_commandline("dotnet-format", config)
    assert resolved.commandline[:2] == ["mise", "exec"]


def test_command_runner_validation_rejects_unknown_value(tmp_path: pathlib.Path) -> None:
    """`{command}-runner`に不正値を与えるとload_configがエラーで弾く。"""
    (tmp_path / "pyproject.toml").write_text('[tool.pyfltr]\ntypos-runner = "bogus"\n')
    with pytest.raises(ValueError, match="typos-runner"):
        pyfltr.config.config.load_config(config_dir=tmp_path)


# --- mise設定記述判定によるtool spec省略仕様 ---


def test_build_commandline_omits_tool_spec_when_mise_config_has_rust(monkeypatch: pytest.MonkeyPatch) -> None:
    """mise設定に `rust` 記述ありかつversion既定値ならtool spec省略形を返す。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    # tool spec省略形: `mise exec -- cargo` で起動し、mise設定の解決済み内容に従わせる。
    assert resolved.commandline == ["mise", "exec", "--", "cargo"]
    assert resolved.effective_runner == "mise"
    assert resolved.tool_spec_omitted is True


def test_build_commandline_omits_tool_spec_when_mise_config_has_aqua_cargo_deny(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mise設定にaqua表記の `aqua:EmbarkStudios/cargo-deny` 記述ありなら省略形になる。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"aqua:EmbarkStudios/cargo-deny": [{"version": "0.16.0"}]}
        ),
    )
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-deny", config)
    assert resolved.commandline == ["mise", "exec", "--", "cargo-deny"]
    assert resolved.tool_spec_omitted is True


def test_build_commandline_keeps_tool_spec_when_mise_config_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """判定辞書が空（記述なし）の場合は従来形 `<backend>@latest` を組み立てる。"""
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )
    config = pyfltr.config.config.create_default_config()
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["mise", "exec", "rust@latest", "--", "cargo"]
    assert resolved.tool_spec_omitted is False


def test_build_commandline_keeps_tool_spec_when_version_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`{command}-version` を具体値で指定した場合は判定結果に関わらず従来形を組み立てる。"""
    # 判定辞書には `rust` 記述があるが、versionが明示されているので利用者の意図を尊重する。
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(
            status="ok", tools={"rust": [{"version": "1.83.0"}]}
        ),
    )
    config = pyfltr.config.config.create_default_config()
    config.values["cargo-fmt-version"] = "1.84.0"
    resolved = pyfltr.command.runner.build_commandline("cargo-fmt", config)
    assert resolved.commandline == ["mise", "exec", "rust@1.84.0", "--", "cargo"]
    assert resolved.tool_spec_omitted is False


def test_build_commandline_allow_side_effects_propagates_to_active_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_commandline` の `allow_side_effects` が `get_mise_active_tools` に連動して渡る。"""
    received: list[bool] = []

    def fake(
        config: pyfltr.config.config.Config, *, allow_side_effects: bool = False
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config
        received.append(allow_side_effects)
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", fake)
    config = pyfltr.config.config.create_default_config()
    pyfltr.command.runner.build_commandline("cargo-fmt", config, allow_side_effects=True)
    pyfltr.command.runner.build_commandline("cargo-fmt", config, allow_side_effects=False)
    pyfltr.command.runner.build_commandline("cargo-fmt", config)  # 既定値はFalse
    assert received == [True, False, False]


# --- ensure_mise_available のtool spec有無分岐 ---


def test_ensure_mise_available_check_args_with_tool_spec(mocker) -> None:
    """tool spec組立形は `mise exec <tool_spec> -- <bin> --version` を呼び出す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )
    resolved = pyfltr.command.runner.ResolvedCommandline(
        executable="mise",
        prefix=["exec", "rust@latest", "--", "cargo"],
        runner="bin-runner",
        runner_source="default",
        effective_runner="mise",
    )
    config = pyfltr.config.config.create_default_config()
    pyfltr.command.runner.ensure_mise_available(resolved, config, command="cargo-fmt")
    assert mock_run.call_args.args[0] == ["mise", "exec", "rust@latest", "--", "cargo", "--version"]


def test_ensure_mise_available_check_args_without_tool_spec(mocker) -> None:
    """tool spec省略形は `mise exec -- <bin> --version` を呼び出す。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mock_run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=0, stdout="", stderr=""),
    )
    resolved = pyfltr.command.runner.ResolvedCommandline(
        executable="mise",
        prefix=["exec", "--", "cargo"],
        runner="bin-runner",
        runner_source="default",
        effective_runner="mise",
    )
    config = pyfltr.config.config.create_default_config()
    pyfltr.command.runner.ensure_mise_available(resolved, config, command="cargo-fmt")
    assert mock_run.call_args.args[0] == ["mise", "exec", "--", "cargo", "--version"]


def test_ensure_mise_available_error_message_without_tool_spec(mocker) -> None:
    """tool spec省略形での失敗時、エラー文面が `mise exec -- <bin>: ...` 形になる。"""
    mocker.patch("shutil.which", return_value="/usr/local/bin/mise")
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(["mise"], returncode=1, stdout="", stderr="mise ERROR could not resolve tool"),
    )
    resolved = pyfltr.command.runner.ResolvedCommandline(
        executable="mise",
        prefix=["exec", "--", "cargo"],
        runner="bin-runner",
        runner_source="default",
        effective_runner="mise",
    )
    config = pyfltr.config.config.create_default_config()
    with pytest.raises(FileNotFoundError) as excinfo:
        pyfltr.command.runner.ensure_mise_available(resolved, config, command="cargo-fmt")
    message = str(excinfo.value)
    assert "mise exec -- cargo" in message
    assert "could not resolve tool" in message


# --- get_mise_active_tools のキャッシュキー差分 ---


def test_get_mise_active_tools_cache_key_differs_by_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """cwd差分で別キャッシュエントリとして扱う。

    `os.chdir()` 等でcwdが切り替わった場合、`mise ls --current --json` の結果は変わるため、
    プロセス内キャッシュもcwdをキーに含める必要がある。
    """
    # autouseフィクスチャは `get_mise_active_tools` 自体をモック上書きしているため、
    # 実装本体を直接検証するためconftestが保持する元参照に戻し、`_query_mise_active_tools` のみ
    # fakeへ差し替える。
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    call_log: list[str] = []

    def fake_query(
        config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config, allow_side_effects
        call_log.append(os.getcwd())
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise._query_mise_active_tools", fake_query)
    config = pyfltr.config.config.create_default_config()

    cwd_a = tmp_path / "a"
    cwd_b = tmp_path / "b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    monkeypatch.chdir(cwd_a)
    pyfltr.command.mise.get_mise_active_tools(config)
    monkeypatch.chdir(cwd_a)
    pyfltr.command.mise.get_mise_active_tools(config)  # 同cwd → キャッシュヒット
    monkeypatch.chdir(cwd_b)
    pyfltr.command.mise.get_mise_active_tools(config)  # 別cwd → 再呼び出し
    assert len(call_log) == 2


def test_get_mise_active_tools_cache_key_differs_by_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """`MISE_CONFIG_FILE` 等のenv差分で別キャッシュエントリとして扱う。"""
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.chdir(tmp_path)
    call_count = [0]

    def fake_query(
        config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config, allow_side_effects
        call_count[0] += 1
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise._query_mise_active_tools", fake_query)
    config = pyfltr.config.config.create_default_config()

    monkeypatch.delenv("MISE_CONFIG_FILE", raising=False)
    pyfltr.command.mise.get_mise_active_tools(config)
    monkeypatch.setenv("MISE_CONFIG_FILE", "/tmp/other.toml")  # noqa: S108  # テスト内のダミーパス（実ファイルアクセスなし）
    pyfltr.command.mise.get_mise_active_tools(config)  # env差分 → 再呼び出し
    assert call_count[0] == 2


def test_get_mise_active_tools_cache_key_differs_by_allow_side_effects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """副作用許可フラグ差分で別キャッシュエントリとして扱う。

    副作用OFFで保存したフォールバック結果が、後続の副作用ON呼び出しで正規化される
    流れに対応するため、フラグ自体もキーに含める。
    """
    monkeypatch.setattr("pyfltr.command.mise.get_mise_active_tools", _testconf.real_get_mise_active_tools)
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.chdir(tmp_path)

    received_flags: list[bool] = []

    def fake_query(
        config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> pyfltr.command.mise.MiseActiveToolsResult:
        del config
        received_flags.append(allow_side_effects)
        if allow_side_effects:
            return pyfltr.command.mise.MiseActiveToolsResult(status="ok", tools={"rust": []})
        return pyfltr.command.mise.MiseActiveToolsResult(status="ok")

    monkeypatch.setattr("pyfltr.command.mise._query_mise_active_tools", fake_query)
    config = pyfltr.config.config.create_default_config()

    result_off = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=False)
    result_on = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=True)
    assert not result_off.tools
    assert result_on.tools == {"rust": []}
    assert received_flags == [False, True]
    # 同じフラグでの2回目はキャッシュヒット。
    pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=True)
    assert received_flags == [False, True]


# --- _query_mise_active_tools のステータス分類 ---


def test_query_mise_active_tools_mise_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mise` がPATH上に存在しないときは `mise-not-found` ステータスを返す。"""
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: None if name == "mise" else "/bin/" + name)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise._query_mise_active_tools(config, allow_side_effects=False)
    assert result.status == "mise-not-found"
    assert not result.tools


def test_query_mise_active_tools_untrusted_no_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """副作用OFF下で未信頼config由来エラーが出たときは `untrusted-no-side-effects` を返す。"""
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str], mise_env: dict[str, str], config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects
        return 1, "", "Error: config /home/u/mise.toml is not trusted", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise._query_mise_active_tools(config, allow_side_effects=False)
    assert result.status == "untrusted-no-side-effects"
    assert result.detail is not None
    assert "not trusted" in result.detail


def test_query_mise_active_tools_trust_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """trust試行が拒否されたら `trust-failed` を返す。"""
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str], mise_env: dict[str, str], config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects
        return 2, "", "trust rejected by user", True

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise._query_mise_active_tools(config, allow_side_effects=True)
    assert result.status == "trust-failed"
    assert result.detail is not None
    assert "trust rejected" in result.detail


def test_query_mise_active_tools_exec_error_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """`OSError` 例外が出たときは `exec-error` ステータスを返す。"""
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str], mise_env: dict[str, str], config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects
        raise OSError("mise binary missing executable bit")

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise._query_mise_active_tools(config, allow_side_effects=False)
    assert result.status == "exec-error"
    assert result.detail is not None
    assert "executable bit" in result.detail


def test_query_mise_active_tools_json_parse_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mise ls` のstdoutがJSONとしてパースできなければ `json-parse-error` を返す。"""
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str], mise_env: dict[str, str], config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects
        return 0, "this is not json", "", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise._query_mise_active_tools(config, allow_side_effects=False)
    assert result.status == "json-parse-error"


def test_query_mise_active_tools_unexpected_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSONがdict以外（list等）のときは `unexpected-shape` を返す。"""
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str], mise_env: dict[str, str], config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects
        return 0, "[]", "", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise._query_mise_active_tools(config, allow_side_effects=False)
    assert result.status == "unexpected-shape"
    assert result.detail == "got list"


def test_query_mise_active_tools_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """正常取得時は `ok` ステータスとtoolsを返す。"""
    monkeypatch.setattr("pyfltr.command.mise.shutil.which", lambda name: f"/usr/local/bin/{name}")

    def fake_run_with_trust(
        args: list[str], mise_env: dict[str, str], config: pyfltr.config.config.Config, *, allow_side_effects: bool
    ) -> tuple[int, str, str, bool]:
        del args, mise_env, config, allow_side_effects
        return 0, '{"rust": [{"version": "1.83.0"}]}', "", False

    monkeypatch.setattr("pyfltr.command.mise.run_mise_with_trust", fake_run_with_trust)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.mise._query_mise_active_tools(config, allow_side_effects=False)
    assert result.status == "ok"
    assert result.tools == {"rust": [{"version": "1.83.0"}]}


# --- get_mise_active_tool_key 公開API ---


def test_get_mise_active_tool_key_for_cargo_fmt() -> None:
    """rust backendを使うcargo-fmtは `rust` を返す。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("cargo-fmt") == "rust"


def test_get_mise_active_tool_key_for_cargo_deny() -> None:
    """cargo-denyは `aqua:EmbarkStudios/cargo-deny` を返す（mise.toml記述に合わせた形）。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("cargo-deny") == "aqua:EmbarkStudios/cargo-deny"


def test_get_mise_active_tool_key_for_simple_tool() -> None:
    """`mise_backend` 未設定のツールは `bin_name` をそのまま返す。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("actionlint") == "actionlint"


def test_get_mise_active_tool_key_for_unknown_command() -> None:
    """mise backend未登録のコマンドは `None` を返す。"""
    assert pyfltr.command.runner.get_mise_active_tool_key("ruff-check") is None
    assert pyfltr.command.runner.get_mise_active_tool_key("not-registered") is None
