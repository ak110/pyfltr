"""command.py の textlint fix テスト。

``_execute_textlint_fix`` の動作を検証する。
"""

# pylint: disable=protected-access,duplicate-code

import os
import pathlib
import subprocess

import pyfltr.command
import pyfltr.config
import pyfltr.paths
import pyfltr.warnings_
from tests import conftest as _testconf


def test_textlint_lint_mode_adds_lint_args(mocker, tmp_path: pathlib.Path) -> None:
    """非 fix モードで textlint-lint-args (既定は --format compact) が commandline に追加される。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.execute_command("textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    # textlint-json=True（既定）により、lint-args の compact が json に置換される
    assert "json" in cmdline
    fmt_idx = cmdline.index("--format")
    assert cmdline[fmt_idx + 1] == "json"


def test_textlint_fix_mode_two_step_execution(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで textlint は 2 段階実行される (step1: fix → step2: lint check)。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command._run_subprocess", return_value=proc)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert mock_run.call_count == 2
    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]

    # step1: fix-args (--fix) あり、--format なし (fixer-formatter は compact をサポートしないため)
    assert "--fix" in step1_cmdline
    assert "--format" not in step1_cmdline
    # step2: 構造化出力注入により --format json あり、--fix なし
    assert "--fix" not in step2_cmdline
    assert "--format" in step2_cmdline
    assert "json" in step2_cmdline


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
    pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

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
    pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

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

    def fake_run(cmdline, env, on_output, **_kwargs):
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
    result = pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "succeeded"
    assert result.has_error is False


def test_textlint_fix_mode_all_fixed_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで全件修正され残存違反なしなら formatted (内容ハッシュに変化あり)。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    call_count = [0]

    def fake_run(cmdline, env, on_output, **_kwargs):
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
    result = pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "formatted"
    assert result.has_error is False


def test_textlint_fix_mode_emits_warning_when_protected_identifier_corrupted(mocker, tmp_path: pathlib.Path) -> None:
    """保護対象識別子 (.NET など) が fix で全角化された場合、warning が発行される。"""
    target = tmp_path / "sample.md"
    target.write_text("本文で.NET系の話題を扱う。\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            # preset-jtf-style が「.」を「。」へ変換したことを模擬
            target.write_text("本文で。NET系の話題を扱う。\n")
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "textlint-identifier-corruption"]
    assert len(entries) == 1
    assert ".NET" in entries[0]["message"]
    # パスは cwd 相対化されて記録される（絶対パスのまま埋め込まれない）
    relative = pyfltr.paths.to_cwd_relative(target)
    assert f"file={relative}" in entries[0]["message"]
    # hint として恒久対策が添えられる
    assert "バックティック" in entries[0]["hint"]


def test_textlint_fix_mode_no_warning_when_protected_identifiers_empty(mocker, tmp_path: pathlib.Path) -> None:
    """textlint-protected-identifiers が空なら検知をスキップし warning は出ない。"""
    target = tmp_path / "sample.md"
    target.write_text("本文で.NET系の話題を扱う。\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            target.write_text("本文で。NET系の話題を扱う。\n")
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-protected-identifiers"] = []
    pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "textlint-identifier-corruption"]
    assert not entries


def test_textlint_fix_mode_no_warning_when_identifier_intact(mocker, tmp_path: pathlib.Path) -> None:
    """fix で他の部分は変わっても、保護対象識別子が維持されていれば warning は出ない。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n\n本文.NETと普通の文.\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            # .NET は保持、末尾の . のみ全角化
            target.write_text("# title\n\n本文.NETと普通の文。\n")
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "textlint-identifier-corruption"]
    assert not entries


def test_textlint_fix_mode_residual_violations_mark_failed(mocker, tmp_path: pathlib.Path) -> None:
    """fix モードで残存違反がある場合は failed、errors が compact 形式でパースされる。"""
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    violation_file = str(target)
    violation_output = f"{violation_file}: line 3, col 5, Error - No mixed period (ja-no-mixed-period)"

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            # step1: fix 適用したが違反が残る (textlint は rc=1 を返すことがある)
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        # step2: compact 形式で違反出力
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout=violation_output)

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

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

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # noqa
        if "--fix" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=2, stdout="fatal error")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command._run_subprocess", side_effect=fake_run)

    config = pyfltr.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "failed"
    assert result.has_error is True
