"""command.py のテスト。"""

# pylint: disable=protected-access,too-many-lines

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


def test_prettier_two_step_check_clean(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 (prettier --check) rc=0 → succeeded。Step2 (--write) は実行されない。"""
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    proc = subprocess.CompletedProcess(["prettier"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.execute_command("prettier", _make_args([target]), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--check" in cmdline
    assert "--write" not in cmdline
    assert result.status == "succeeded"
    assert result.has_error is False


def test_prettier_two_step_check_needs_write(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 → Step2 (--write) を実行。rc=0 なら formatted。"""
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "--check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="[warn] sample.js")
        # --write step
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="sample.js")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.execute_command("prettier", _make_args([target]), config)

    assert result.status == "formatted"
    assert result.has_error is False


def test_prettier_two_step_check_rc2_fails_without_write(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc>=2 (致命的エラー) → failed、Step2 は実行しない。"""
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    calls: list[list[str]] = []

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        calls.append(cmdline)
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="SyntaxError")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.execute_command("prettier", _make_args([target]), config)

    assert result.status == "failed"
    assert result.has_error is True
    # Step2 は実行されない
    assert len(calls) == 1
    assert "--check" in calls[0]


def test_prettier_two_step_step2_failure_marks_failed(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 でも Step2 の rc>=2 なら failed。"""
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        if "--check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=2, stdout="write failed")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.execute_command("prettier", _make_args([target]), config)

    assert result.status == "failed"
    assert result.has_error is True


def test_prettier_fix_mode_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードでは Step1 (--check) をスキップし直接 --write を実行する。"""
    target = tmp_path / "sample.js"
    target.write_text("x=1;\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output):
        del env, on_output  # noqa
        # --write 実行時にファイルを書き換えたことをシミュレート
        target.write_text("x = 1;\n")
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mock_run = mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.execute_command("prettier", _make_args([target], fix=True), config)

    # 1 回だけ呼ばれる (Step1 スキップ)
    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--write" in cmdline
    assert "--check" not in cmdline
    # ハッシュ変化ありなので formatted
    assert result.status == "formatted"


def test_prettier_fix_mode_no_change_succeeds(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードで --write が走ってもハッシュ変化が無ければ succeeded。"""
    target = tmp_path / "sample.js"
    target.write_text("x = 1;\n")

    proc = subprocess.CompletedProcess(["prettier"], returncode=0, stdout="")
    mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["prettier"] = True
    result = pyfltr.command.execute_command("prettier", _make_args([target], fix=True), config)

    assert result.status == "succeeded"


def test_eslint_lint_mode_uses_json_format(mocker, tmp_path: pathlib.Path) -> None:
    """eslint の通常実行で `--format json` (共通 args) が commandline に含まれる。"""
    target = tmp_path / "sample.js"
    target.write_text("var x = 1;\n")

    proc = subprocess.CompletedProcess(["eslint"], returncode=0, stdout="[]")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["eslint"] = True
    pyfltr.command.execute_command("eslint", _make_args([target]), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "json" in cmdline
    fmt_idx = cmdline.index("--format")
    assert cmdline[fmt_idx + 1] == "json"
    # lint モードでは --fix は付かない
    assert "--fix" not in cmdline


def test_eslint_fix_mode_appends_fix_and_keeps_json(mocker, tmp_path: pathlib.Path) -> None:
    """eslint の fix モードで `--fix` が付いても `--format json` は維持される (共通 args 経由)。"""
    target = tmp_path / "sample.js"
    target.write_text("var x = 1;\n")

    proc = subprocess.CompletedProcess(["eslint"], returncode=0, stdout="[]")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["eslint"] = True
    pyfltr.command.execute_command("eslint", _make_args([target], fix=True), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "json" in cmdline
    assert "--fix" in cmdline
    # --format json が --fix より前 (共通 args 先行)
    assert cmdline.index("--format") < cmdline.index("--fix")


def test_biome_lint_mode_uses_check_and_github_reporter(mocker, tmp_path: pathlib.Path) -> None:
    """biome の通常実行で `check` サブコマンドと `--reporter=github` が含まれる。"""
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["biome"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["biome"] = True
    pyfltr.command.execute_command("biome", _make_args([target]), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "--reporter=github" in cmdline
    assert "--write" not in cmdline


def test_biome_fix_mode_appends_write_and_keeps_reporter(mocker, tmp_path: pathlib.Path) -> None:
    """biome の fix モードで `--write` が付いても `--reporter=github` は維持される。"""
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["biome"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["biome"] = True
    pyfltr.command.execute_command("biome", _make_args([target], fix=True), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "--reporter=github" in cmdline
    assert "--write" in cmdline
    # check と --reporter=github は共通 args なので --write より前
    assert cmdline.index("check") < cmdline.index("--write")
    assert cmdline.index("--reporter=github") < cmdline.index("--write")


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
    """pnpx runner では textlint-packages が --package で展開される。

    textlint 本体の spec は `_JS_TOOL_PNPX_PACKAGE_SPEC` によって
    既知バグのあるバージョンを除外した形で指定される。
    """
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"
    config.values["textlint-packages"] = ["textlint-rule-preset-ja-technical-writing", "textlint-rule-ja-no-abusage"]

    path, prefix = pyfltr.command._resolve_js_commandline("textlint", config)

    assert path == "pnpx"
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

    assert path == "pnpx"
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

    assert path == "pnpx"
    assert prefix == ["--package", "markdownlint-cli2", "markdownlint-cli2"]


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


def test_resolve_js_commandline_pnpx_eslint() -> None:
    """pnpx runner で eslint が通常通り (bin 名 = パッケージ名) 解決される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("eslint", config)

    assert path == "pnpx"
    assert prefix == ["--package", "eslint", "eslint"]


def test_resolve_js_commandline_pnpx_prettier() -> None:
    """pnpx runner で prettier が通常通り解決される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("prettier", config)

    assert path == "pnpx"
    assert prefix == ["--package", "prettier", "prettier"]


def test_resolve_js_commandline_pnpx_biome_uses_scoped_package() -> None:
    """pnpx runner で biome はスコープ付きパッケージ @biomejs/biome で解決される。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpx"

    path, prefix = pyfltr.command._resolve_js_commandline("biome", config)

    assert path == "pnpx"
    # --package には @biomejs/biome、bin 名は biome
    assert prefix == ["--package", "@biomejs/biome", "biome"]


def test_resolve_js_commandline_pnpm_prettier() -> None:
    """pnpm runner で prettier が pnpm exec prettier になる。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command._resolve_js_commandline("prettier", config)

    assert path == "pnpm"
    assert prefix == ["exec", "prettier"]


def test_resolve_js_commandline_pnpm_biome() -> None:
    """pnpm runner で biome が pnpm exec biome になる (スコープ無効)。"""
    config = pyfltr.config.create_default_config()
    config.values["js-runner"] = "pnpm"

    path, prefix = pyfltr.command._resolve_js_commandline("biome", config)

    assert path == "pnpm"
    assert prefix == ["exec", "biome"]


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
    # 併せて、pnpm が mise の shim 経由で呼ばれる環境では HOME 切り替えにより
    # 既存の trust 情報が見えなくなり、グローバル mise config の読込が
    # 「trusted でない」として失敗する。切り替え前の HOME 配下の config を
    # 明示的に信頼させて回避する。
    original_home = pathlib.Path(os.environ["HOME"])
    mise_config = original_home / ".config" / "mise" / "config.toml"
    monkeypatch.setenv("MISE_TRUSTED_CONFIG_PATHS", str(mise_config))
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


def test_expand_globs_respects_gitignore(tmp_path: pathlib.Path) -> None:
    """.gitignore に記載されたファイルが expand_globs から除外される。"""
    # git リポジトリを初期化
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    (tmp_path / "main.py").write_text("x = 1\n")
    (tmp_path / "ignored.py").write_text("x = 2\n")
    (tmp_path / ".gitignore").write_text("ignored.py\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.create_default_config()
        result = pyfltr.command.expand_globs([], ["*.py"], config)
        names = {p.name for p in result}
        assert "main.py" in names
        assert "ignored.py" not in names
    finally:
        os.chdir(original_cwd)


def test_expand_globs_gitignore_disabled(tmp_path: pathlib.Path) -> None:
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
        result = pyfltr.command.expand_globs([], ["*.py"], config)
        names = {p.name for p in result}
        assert "main.py" in names
        assert "ignored.py" in names
    finally:
        os.chdir(original_cwd)


def test_expand_globs_no_git_repo(tmp_path: pathlib.Path) -> None:
    """git リポジトリ外でも正常に動作する。"""
    (tmp_path / "main.py").write_text("x = 1\n")

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        config = pyfltr.config.create_default_config()
        result = pyfltr.command.expand_globs([], ["*.py"], config)
        names = {p.name for p in result}
        assert "main.py" in names
    finally:
        os.chdir(original_cwd)


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
    args = _make_args([target])
    result = pyfltr.command.execute_command("pylint", args, config)
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
            ["mise", "exec", "typos@latest", "--", "typos", "--version"],
            returncode=0,
            stdout="typos 1.0.0",
            stderr="",
        ),
    )

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"

    path, prefix = pyfltr.command._resolve_bin_commandline("typos", config)

    assert path == "mise"
    assert prefix == ["exec", "typos@latest", "--", "typos"]


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


def test_resolve_bin_commandline_mise_not_installed(mocker) -> None:
    """miseモードでmiseがPATHに無い場合、FileNotFoundErrorを送出する。"""
    mocker.patch("shutil.which", return_value=None)

    config = pyfltr.config.create_default_config()
    config.values["bin-runner"] = "mise"

    with pytest.raises(FileNotFoundError, match="mise"):
        pyfltr.command._resolve_bin_commandline("actionlint", config)


def test_resolve_bin_commandline_mise_tool_not_installed(mocker) -> None:
    """miseモードでツールが未インストールの場合、FileNotFoundErrorを送出する。"""
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

    with pytest.raises(FileNotFoundError, match="mise exec"):
        pyfltr.command._resolve_bin_commandline("ec", config)


def test_failed_resolution_result() -> None:
    """_failed_resolution_resultが失敗用のCommandResultを返す。"""
    command_info = pyfltr.config.CommandInfo(type="linter")

    result = pyfltr.command._failed_resolution_result("shellcheck", command_info, "ツールが見つかりません: shellcheck")

    assert result.returncode == 1
    assert result.has_error is True
    assert result.status == "failed"
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

    result = pyfltr.command.execute_command("tsc", _make_args([target]), config)

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
    result = pyfltr.command.execute_command("ruff-check", _make_args([target]), config)

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    # ファイルパスがコマンドラインに含まれることを確認
    assert str(target) in cmdline
    assert result.status == "succeeded"


def test_bin_tool_spec_all_tools_defined() -> None:
    """_BIN_TOOL_SPECに全bin系ツールが定義されている。"""
    expected_tools = {"ec", "shellcheck", "shfmt", "typos", "actionlint"}
    assert set(pyfltr.command._BIN_TOOL_SPEC.keys()) == expected_tools


def test_bin_tool_spec_structure() -> None:
    """BinToolSpecのフィールドが正しく設定されている。"""
    spec = pyfltr.command._BIN_TOOL_SPEC["ec"]
    assert spec.bin_name == "ec"
    assert spec.mise_backend == "editorconfig-checker"
    assert spec.default_version == "latest"

    spec = pyfltr.command._BIN_TOOL_SPEC["shellcheck"]
    assert spec.bin_name == "shellcheck"
