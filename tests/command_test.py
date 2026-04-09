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
    """プロジェクトが上書きした {command}-args が fix モードでも保持される (置換されない)。

    markdownlint は単発 fix 経路を通るため、通常 args の後に fix-args が append される。
    """
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["markdownlint-cli2"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["markdownlint"] = True
    config.values["markdownlint-args"] = ["--config", "custom.yaml"]
    pyfltr.command.execute_command("markdownlint", _make_args([target], fix=True), config)

    cmdline = mock_run.call_args_list[0][0][0]
    # 通常 args が残っている
    assert "--config" in cmdline
    assert "custom.yaml" in cmdline
    # fix-args も追加されている
    assert "--fix" in cmdline
    # 順序: 通常 args は --fix より前
    assert cmdline.index("custom.yaml") < cmdline.index("--fix")


def test_textlint_lint_mode_adds_lint_args(mocker, tmp_path: pathlib.Path) -> None:
    """非 fix モードで textlint-lint-args (既定は --format compact) が commandline に追加される。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.execute_command("textlint", _make_args([target]), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "compact" in cmdline
    fmt_idx = cmdline.index("--format")
    assert cmdline[fmt_idx + 1] == "compact"


def test_textlint_fix_mode_two_step_execution(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで textlint は 2 段階実行される (step1: fix → step2: lint check)。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    assert mock_run.call_count == 2
    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]

    # step1: fix-args (--fix) あり、--format なし (fixer-formatter は compact をサポートしないため)
    assert "--fix" in step1_cmdline
    assert "--format" not in step1_cmdline
    # step2: lint-args (--format compact) あり、--fix なし
    assert "--fix" not in step2_cmdline
    assert "--format" in step2_cmdline
    assert "compact" in step2_cmdline


def test_textlint_fix_mode_strips_user_format_from_step1(mocker, tmp_path: pathlib.Path) -> None:
    """ユーザーが textlint-args に --format を設定していても step1 では除去される (下位互換)。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    # 旧 docs で推奨されていた設定: textlint-args に --format compact を含む
    config.values["textlint-args"] = ["--format", "compact"]
    pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    assert mock_run.call_count == 2
    step1_cmdline = mock_run.call_args_list[0][0][0]
    # step1: --format / compact が物理的に除去されている (fixer-formatter 互換性のため)
    assert "--format" not in step1_cmdline
    assert "compact" not in step1_cmdline
    assert "--fix" in step1_cmdline


def test_textlint_fix_mode_preserves_non_format_user_args(mocker, tmp_path: pathlib.Path) -> None:
    """ユーザーが textlint-args に追加した --format 以外のオプションは両ステップで保持される。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-args"] = ["--quiet"]
    pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]
    assert "--quiet" in step1_cmdline
    assert "--quiet" in step2_cmdline


def test_textlint_fix_mode_touch_without_content_change_marks_succeeded(mocker, tmp_path: pathlib.Path) -> None:
    """textlint が内容を変えずにファイルを書き戻した場合は succeeded 扱いになる。

    textlint --fix は残存違反がなくても対象ファイルを touch することがあり、
    mtime ベースで検知すると偽陽性 (formatted) になってしまう。内容ハッシュで
    比較することで、真の修正がない限り succeeded が維持されることを担保する。
    """
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            # step1: 内容は変えず mtime だけ更新 (textlint の touch 挙動を模擬)
            os.utime(target, (2000000000, 2000000000))
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        # step2: 残存違反なし
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    assert result.status == "succeeded"
    assert result.has_error is False


def test_textlint_fix_mode_all_fixed_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで全件修正され残存違反なしなら formatted (内容ハッシュに変化あり)。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    call_count = [0]

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        call_count[0] += 1
        if call_count[0] == 1:
            # step1: fix 適用 (mtime 更新)
            target.write_text("# Title\n")
            os.utime(target, (2000000000, 2000000000))
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        # step2: 残存違反なし
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    assert result.status == "formatted"
    assert result.has_error is False


def test_textlint_fix_mode_residual_violations_mark_failed(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで残存違反がある場合は failed、errors が compact 形式でパースされる。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    violation_file = str(target)
    violation_output = f"{violation_file}: line 3, col 5, Error - No mixed period (ja-no-mixed-period)"

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            # step1: fix 適用したが違反が残る (textlint は rc=1 を返すことがある)
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        # step2: compact 形式で違反出力
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout=violation_output)

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    assert result.status == "failed"
    assert result.has_error is True
    assert len(result.errors) == 1
    assert result.errors[0].line == 3
    assert result.errors[0].col == 5
    assert "ja-no-mixed-period" in result.errors[0].message


def test_textlint_fix_mode_step1_fatal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    """step1 の rc >= 2 (致命的エラー) は step2 の結果にかかわらず failed 扱い。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=2, stdout="fatal error")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.execute_command("textlint", _make_args([target], fix=True), config)

    assert result.status == "failed"
    assert result.has_error is True


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


def test_resolve_js_commandline_pnpx_with_textlint_packages() -> None:
    """pnpx runner では textlint-packages が --package で展開される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing", "textlint-rule-ja-no-abusage"]

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert path == "pnpx"
    assert prefix == [
        "--package",
        "textlint",
        "--package",
        "textlint-rule-preset-ja-technical-writing",
        "--package",
        "textlint-rule-ja-no-abusage",
        "textlint",
    ]


def test_resolve_js_commandline_pnpm_ignores_packages() -> None:
    """pnpm runner では textlint-packages は無視される (package.json 側で管理前提)。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert path == "pnpm"
    assert prefix == ["exec", "textlint"]


def test_resolve_js_commandline_markdownlint_uses_cli2_binary() -> None:
    """markdownlint コマンドの実体は markdownlint-cli2。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command._resolve_js_commandline("markdownlint", config)

    assert path == "pnpm"
    assert prefix == ["exec", "markdownlint-cli2"]


def test_resolve_js_commandline_npx() -> None:
    """npx runner では -p でパッケージを指定する。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "npx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing"]

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert path == "npx"
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
    """js-runner=direct で実行ファイル不在時、例外でなく failed CommandResult を返す。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "direct"
    config.values["textlint"] = True

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        result = pyfltr.command.execute_command("textlint", _make_args([target]), config)
        assert result.status == "failed"
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
