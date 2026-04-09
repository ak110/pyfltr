"""command.py のテスト。"""

# pylint: disable=protected-access

import argparse
import os
import pathlib
import shutil
import subprocess

import pytest

import pyfltr.command
import pyfltr.config


def _make_args(targets: list[pathlib.Path], *, fix: bool = False) -> argparse.Namespace:
    """execute_command に渡す argparse.Namespace を作成。"""
    return argparse.Namespace(targets=targets, shuffle=False, verbose=False, fix=fix)


def test_ruff_format_two_step_runs_check_and_format(mocker, tmp_path: pathlib.Path) -> None:
    """ruff-format-by-check=true のとき ruff check と ruff format の両方が実行される。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.execute_command("ruff-format", _make_args([target]), config)

    # subprocess は 2 回呼ばれる (check ステップ + format ステップ)
    assert mock_run.call_count == 2
    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]
    assert "check" in step1_cmdline
    assert "--fix" in step1_cmdline
    assert "--unsafe-fixes" in step1_cmdline
    assert "format" in step2_cmdline
    assert "--exit-non-zero-on-format" in step2_cmdline
    # status はどちらも exit 0 なので succeeded
    assert result.status == "succeeded"


def test_ruff_format_by_check_false_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    """ruff-format-by-check=false のとき ruff format のみが実行される。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    config.values["ruff-format-by-check"] = False
    result = pyfltr.command.execute_command("ruff-format", _make_args([target]), config)

    # subprocess は 1 回のみ (format ステップのみ)
    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "format" in cmdline
    assert "check" not in cmdline
    assert result.status == "succeeded"


def test_ruff_format_step1_lint_violation_ignored(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ1の lint violation (exit 1) は失敗扱いしない。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="lint violation")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.execute_command("ruff-format", _make_args([target]), config)

    # ステップ1の exit 1 は無視され、ステップ2の exit 0 が反映されて succeeded
    assert result.status == "succeeded"
    assert result.has_error is False


def test_ruff_format_step1_internal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ1の exit 2 (設定ミス等) は failed 扱い。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=2, stdout="usage error")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.execute_command("ruff-format", _make_args([target]), config)

    assert result.status == "failed"
    assert result.has_error is True


def test_ruff_format_step2_internal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ2の exit 2 も failed 扱い。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="format error")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.execute_command("ruff-format", _make_args([target]), config)

    assert result.status == "failed"
    assert result.has_error is True


def test_ruff_format_step1_mtime_change_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    """ステップ1でファイルが書き換わった場合、formatted 扱いになる。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")
    # ファイルシステムの mtime 分解能の影響で同一ナノ秒に収まるケースを避けるため、
    # 事前に古めの mtime を設定しておく (テストの決定性担保)。
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "check" in cmdline:
            # ruff check が修正を適用したことをシミュレート: 明示的に新しい mtime を設定。
            target.write_text("x = 2\n")
            os.utime(target, (2000000000, 2000000000))
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.execute_command("ruff-format", _make_args([target]), config)

    # mtime が変化したので formatted
    assert result.status == "formatted"
    assert result.has_error is False


def test_fix_mode_appends_fix_args_for_linter(mocker, tmp_path: pathlib.Path) -> None:
    """fix モード時、linter のコマンドラインに fix-args が追加される。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    result = pyfltr.command.execute_command("markdownlint", _make_args([target], fix=True), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    # 通常 args ("markdownlint-cli2") の後に fix-args ("--fix") が続く
    assert "markdownlint-cli2" in cmdline
    assert "--fix" in cmdline
    assert cmdline.index("markdownlint-cli2") < cmdline.index("--fix")
    # 変更なし + rc=0 なので succeeded
    assert result.status == "succeeded"


def test_fix_mode_preserves_custom_args(mocker, tmp_path: pathlib.Path) -> None:
    """プロジェクトが上書きした {command}-args が fix モードでも保持される (置換されない)。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-args"] = ["--package", "my-package", "textlint", "--format", "json"]
    pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    cmdline = mock_run.call_args_list[0][0][0]
    # 通常 args が残っている
    assert "--package" in cmdline
    assert "my-package" in cmdline
    assert "--format" in cmdline
    assert "json" in cmdline
    # fix-args も追加されている
    assert "--fix" in cmdline
    # 順序: --format json は --fix より前
    assert cmdline.index("json") < cmdline.index("--fix")


def test_fix_mode_mtime_change_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで linter がファイルを書き換えた場合、formatted 扱いになる。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        # fix 適用をシミュレート
        target.write_text("# Title\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    result = pyfltr.command.execute_command("markdownlint", _make_args([target], fix=True), config)

    assert result.status == "formatted"
    assert result.has_error is False


def test_fix_mode_non_zero_rc_is_failed(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで rc != 0 なら mtime に関係なく failed。"""
    # ruff-check の targets は *.py
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        # 一部修正したが未修正の違反が残って rc=1 のケースをシミュレート
        target.write_text("# Title\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout="violation remains")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["ruff-check"] = True
    result = pyfltr.command.execute_command("ruff-check", _make_args([target], fix=True), config)

    # rc != 0 なので mtime 変化があっても failed
    assert result.status == "failed"
    assert result.has_error is True


def test_fix_mode_formatter_uses_normal_path(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードでも formatter (fix-args 未定義) は既存経路を通る。"""
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["ruff"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["ruff-format"] = True
    result = pyfltr.command.execute_command("ruff-format", _make_args([target], fix=True), config)

    # ruff-format は 2 段階実行 (ruff-format-by-check が既定で有効)
    assert mock_run.call_count == 2
    assert result.status == "succeeded"


def test_build_subprocess_env_sets_supply_chain_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """サプライチェーン対策用の環境変数が既定値で注入される。"""
    monkeypatch.delenv("UV_EXCLUDE_NEWER", raising=False)
    monkeypatch.delenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", raising=False)

    config = pyfltr.config.create_default_config()
    env = pyfltr.command._build_subprocess_env(config, "pytest")

    assert env["UV_EXCLUDE_NEWER"] == "1 day"
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "1440"


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


@pytest.mark.skipif(shutil.which("pnpm") is None, reason="pnpm が PATH に無い")
def test_build_subprocess_env_npm_config_actually_effective(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """注入した NPM_CONFIG_MINIMUM_RELEASE_AGE が実際に pnpm に反映されることを確認する。

    環境変数名が typo したり、pnpm の仕様変更で効かなくなったりした場合に検知する。
    既定値 (1440) は実行環境のグローバル pnpm 設定と区別できないため、
    ユーザー既定値優先 (setdefault) の動作を利用して非標準値 4321 を注入し検証する。
    """
    # pnpm の設定ファイル読込を避けるため、隔離した HOME を用意する。
    monkeypatch.setenv("HOME", str(tmp_path))
    # corepack のダウンロードプロンプトを抑止する (環境によっては存在する)。
    monkeypatch.setenv("COREPACK_ENABLE_DOWNLOAD_PROMPT", "0")
    # 非標準値を設定し、_build_subprocess_env がそのまま通すことを利用する。
    monkeypatch.setenv("NPM_CONFIG_MINIMUM_RELEASE_AGE", "4321")

    config = pyfltr.config.create_default_config()
    env = pyfltr.command._build_subprocess_env(config, "markdownlint")
    assert env["NPM_CONFIG_MINIMUM_RELEASE_AGE"] == "4321"

    proc = subprocess.run(
        ["pnpm", "config", "get", "minimumReleaseAge"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "4321"
