import json
import os
import pathlib
import subprocess

import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.paths
import pyfltr.state.cache
import pyfltr.warnings_
from tests import conftest as _testconf


def test_textlint_lint_mode_adds_lint_args(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.dispatcher.execute_command(
        "textlint", _testconf.make_args(), _testconf.make_execution_context(config, [target], start_cwd=tmp_path)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--format" in cmdline
    assert "json" in cmdline
    fmt_idx = cmdline.index("--format")
    assert cmdline[fmt_idx + 1] == "json"


def test_textlint_fix_mode_skips_lint_step_when_fix_succeeds(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path, fail_fast=True),
    )

    assert mock_run.call_count == 1
    step1_cmdline = mock_run.call_args_list[0][0][0]
    assert "--fix" in step1_cmdline
    assert "--format" not in step1_cmdline
    assert result.status == "succeeded"
    assert result.commandline == step1_cmdline


def _fail_fix_step(cmdline, env, on_output, **_kwargs):
    del env, on_output
    return subprocess.CompletedProcess(cmdline, returncode=1 if "--fix" in cmdline else 0, stdout="")


def test_textlint_fix_mode_two_step_execution_when_fix_fails_with_fail_fast(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_fail_fix_step)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path, fail_fast=True),
    )

    assert mock_run.call_count == 2
    step2_cmdline = mock_run.call_args_list[1][0][0]
    assert "--fix" not in step2_cmdline
    assert "--format" in step2_cmdline
    assert "json" in step2_cmdline


def test_textlint_fix_mode_skips_lint_step_when_fix_fails_without_fail_fast(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout="TypeError [ERR_INVALID_ARG_TYPE]")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    assert mock_run.call_count == 1
    assert "--fix" in mock_run.call_args_list[0][0][0]
    assert result.status == "failed"
    assert result.returncode == 1
    assert result.errors == []
    assert "ERR_INVALID_ARG_TYPE" in result.output
    assert result.commandline == mock_run.call_args_list[0][0][0]


def test_textlint_fix_mode_strips_user_format_from_step1(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    proc = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-args"] = ["--format", "compact"]
    pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    assert mock_run.call_count == 1
    step1_cmdline = mock_run.call_args_list[0][0][0]
    # step1: --format / compactが物理的に除去されている（fixer-formatter互換性のため）
    assert "--format" not in step1_cmdline
    assert "compact" not in step1_cmdline
    assert "--fix" in step1_cmdline


def test_textlint_fix_mode_preserves_non_format_user_args(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_fail_fix_step)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-args"] = ["--quiet"]
    pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path, fail_fast=True),
    )

    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]
    assert "--quiet" in step1_cmdline
    assert "--quiet" in step2_cmdline


def test_textlint_fix_mode_touch_without_content_change_marks_succeeded(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--fix" in cmdline:
            os.utime(target, (2000000000, 2000000000))
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    assert result.status == "succeeded"
    assert result.formatter_failed is False


def test_textlint_fix_mode_all_fixed_marks_formatted(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")
    os.utime(target, (1000000000, 1000000000))

    call_count = [0]

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        call_count[0] += 1
        if call_count[0] == 1:
            target.write_text("# Title\n")
            os.utime(target, (2000000000, 2000000000))
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    assert result.status == "formatted"
    assert result.formatter_failed is False


def test_textlint_fix_mode_emits_warning_when_protected_identifier_corrupted(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("本文で.NET系の話題を扱う。\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--fix" in cmdline:
            target.write_text("本文で。NET系の話題を扱う。\n")
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "textlint-identifier-corruption"]
    assert len(entries) == 1
    assert ".NET" in entries[0]["message"]
    relative = pyfltr.paths.to_cwd_relative(target)
    assert f"file={relative}" in entries[0]["message"]
    assert "バックティック" in entries[0]["hint"]


def test_textlint_fix_mode_no_warning_when_protected_identifiers_empty(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("本文で.NET系の話題を扱う。\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--fix" in cmdline:
            target.write_text("本文で。NET系の話題を扱う。\n")
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-protected-identifiers"] = []
    pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "textlint-identifier-corruption"]
    assert not entries


def test_textlint_fix_mode_no_warning_when_identifier_intact(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n\n本文.NETと普通の文.\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--fix" in cmdline:
            target.write_text("# title\n\n本文.NETと普通の文。\n")
            return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    entries = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "textlint-identifier-corruption"]
    assert not entries


def test_textlint_fix_mode_residual_violations_mark_failed(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    violation_file = str(target)
    violation_output = f"{violation_file}: line 3, col 5, Error - No mixed period (ja-no-mixed-period)"

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--fix" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout=violation_output)

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path, fail_fast=True),
    )

    assert result.status == "failed"
    assert result.failed is True
    assert len(result.errors) == 1
    assert result.errors[0].line == 3
    assert result.errors[0].col == 5
    assert "ja-no-mixed-period" in result.errors[0].message


def test_textlint_fix_mode_step1_fatal_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--fix" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=2, stdout="fatal error")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path),
    )

    assert result.status == "failed"
    assert result.failed is True


def test_textlint_fix_mode_step1_exit1_with_clean_lint_fails(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        if "--fix" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="TypeError [ERR_INVALID_ARG_TYPE]")
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="[]")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path, fail_fast=True),
    )

    assert result.status == "failed"
    assert result.returncode == 1
    assert "ERR_INVALID_ARG_TYPE" in result.output
    assert "[]" in result.output


def test_textlint_fix_mode_disables_cache_only_in_fix_step(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# title\n")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_fail_fix_step)

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-args"] = ["--cache"]
    pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], fix_stage=True, start_cwd=tmp_path, fail_fast=True),
    )

    step1_cmdline = mock_run.call_args_list[0][0][0]
    step2_cmdline = mock_run.call_args_list[1][0][0]
    assert step1_cmdline.index("--no-cache") > step1_cmdline.index("--cache")
    assert step1_cmdline[-2] == "--no-cache"
    assert "--cache" in step2_cmdline
    assert "--no-cache" not in step2_cmdline


def test_textlint_fix_cache_only_skips_unchanged_clean_files(mocker, tmp_path: pathlib.Path) -> None:
    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    store = pyfltr.state.cache.CacheStore(tmp_path / "cache")
    clean = tmp_path / "clean.md"
    bad = tmp_path / "bad.md"
    rcfile = tmp_path / ".textlintrc.json"
    clean.write_text("clean")
    bad.write_text("bad")

    def run(cmdline, *_args, **_kwargs):
        results = []
        for target in (clean, bad):
            if not any(pathlib.Path(arg).name == target.name for arg in cmdline):
                continue
            messages = [{"message": "violation"}] if target == bad else []
            applying = []
            if "--fix" in cmdline and target == clean and (target.read_text() == "changed" or rcfile.exists()):
                target.write_text("refixed" if rcfile.exists() else "fixed")
                applying = [{"message": "fixed"}]
            results.append(
                {
                    "filePath": str(target),
                    "output": "本文は出力しない",
                    "messages": messages,
                    "applyingMessages": applying,
                    "remainingMessages": messages,
                }
            )
        return subprocess.CompletedProcess(cmdline, returncode=int(bad.name in cmdline), stdout=json.dumps(results))

    runner = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=run)

    def execute(targets, *, fix_stage=True):
        return pyfltr.command.dispatcher.execute_command(
            "textlint",
            _testconf.make_args(),
            _testconf.make_execution_context(
                config, targets, cache_store=store, cache_run_id="run", fix_stage=fix_stage, start_cwd=tmp_path
            ),
        )

    assert "本文は出力しない" not in execute([clean, bad]).output
    execute([clean, bad], fix_stage=False)
    runner.reset_mock()
    result = execute([clean])
    assert runner.call_count == 0
    assert result.status == "succeeded"
    assert result.target_files == []
    assert execute([clean, bad]).target_files == [bad]
    assert clean.name not in runner.call_args.args[0]

    clean.write_text("changed")
    assert execute([clean, bad]).target_files == [bad, clean]
    assert clean.read_text() == "fixed"
    assert execute([clean, bad]).target_files == [bad, clean]
    execute([clean, bad], fix_stage=False)
    assert execute([clean, bad]).target_files == [bad]
    rcfile.write_text("{}")
    assert execute([clean, bad]).target_files == [bad, clean]
    assert clean.read_text() == "refixed"
