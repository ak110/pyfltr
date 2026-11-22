import argparse
import asyncio
import concurrent.futures
import json
import pathlib
import shutil
import subprocess
import sys
import typing

import mcp.server.mcpserver.exceptions
import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_grep_tools
import pyfltr.cli.mcp_models
import pyfltr.cli.mcp_run_tools
import pyfltr.cli.mcp_runs_tools
import pyfltr.cli.mcp_server
import pyfltr.cli.pipeline
import pyfltr.command.completion
import pyfltr.command.slow_tests
import pyfltr.grep_.history
import pyfltr.grep_.preview
import pyfltr.grep_.replacer
import pyfltr.grep_.types
import pyfltr.paths
import pyfltr.state.archive
import pyfltr.state.runs
from tests.conftest import make_command_result as _make_result
from tests.conftest import seed_archive_run as _seed_run

_NOT_REACHED_OUTCOME = pyfltr.cli.pipeline.PipelineOutcome(
    0,
    None,
    pyfltr.command.completion.RunCompletion(
        completion="not_reached",
        files_reached=0,
        completed_commands=(),
        incomplete_commands=(),
        missing_targets=(),
        fully_excluded_files=(),
    ),
)


_ToolError = mcp.server.mcpserver.exceptions.ToolError

pytestmark = pytest.mark.usefixtures("mcp_cache")


@pytest.mark.asyncio
async def test_tool_run_rejects_invalid_mode() -> None:
    with pytest.raises(_ToolError, match="mode"):
        await pyfltr.cli.mcp_run_tools.tool_run(paths=["dummy"], mode=typing.cast(typing.Any, "invalid"))


@pytest.mark.asyncio
async def test_tool_run_rejects_missing_work_dir(tmp_path: pathlib.Path) -> None:
    with pytest.raises(_ToolError, match="work_dir"):
        await pyfltr.cli.mcp_run_tools.tool_run(
            paths=["dummy"],
            work_dir=str(tmp_path / "missing"),
        )


@pytest.mark.asyncio
async def test_tool_run_resolves_cli_parameters(tmp_path: pathlib.Path, mocker) -> None:
    target = tmp_path / "src" / "sample.py"
    target.parent.mkdir()
    target.write_text("value = 1\n", encoding="utf-8")
    mock_run = mocker.patch("pyfltr.cli.pipeline.run_pipeline", return_value=_NOT_REACHED_OUTCOME)

    await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["src/sample.py"],
        mode="ci",
        commands=["mypy"],
        enable=["mypy"],
        disable=["ec"],
        no_fix=True,
        fail_fast=True,
        only_failed=True,
        changed_since="HEAD",
        work_dir=str(tmp_path),
        shuffle=True,
        exit_zero_even_if_formatted=True,
        jobs=2,
    )

    args, commands, config = mock_run.call_args.args
    kwargs = mock_run.call_args.kwargs
    assert args.include_fix_stage is False
    assert args.shuffle is True
    assert args.exit_zero_even_if_formatted is True
    assert args.targets == [target]
    assert commands == ["mypy"]
    assert config.values["mypy"] is True
    assert config.values["ec"] is False
    assert kwargs["start_cwd"] == tmp_path.resolve()
    assert kwargs["original_sys_args"] == [
        "ci",
        f"--work-dir={tmp_path.resolve()}",
        "--no-fix",
        "--commands=mypy",
        "--enable=mypy",
        "--disable=ec",
        "--shuffle",
        "--exit-zero-even-if-formatted",
        "--jobs=2",
    ]
    assert "--only-failed" not in kwargs["original_sys_args"]
    assert "--changed-since=HEAD" not in kwargs["original_sys_args"]
    assert "--fail-fast" not in kwargs["original_sys_args"]


@pytest.mark.asyncio
async def test_tool_run_flattens_commands(tmp_path: pathlib.Path, mocker) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")
    mock_run = mocker.patch("pyfltr.cli.pipeline.run_pipeline", return_value=_NOT_REACHED_OUTCOME)

    await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(target)],
        commands=["ec,typos", "ec"],
    )

    assert mock_run.call_args.args[1] == ["ec", "typos"]


@pytest.mark.asyncio
async def test_tool_run_no_fix_disables_run_fix_stage(tmp_path: pathlib.Path, mocker) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")
    mock_run = mocker.patch("pyfltr.cli.pipeline.run_pipeline", return_value=_NOT_REACHED_OUTCOME)

    await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(target)],
        mode="run",
        commands=["ec"],
        no_fix=True,
    )

    assert mock_run.call_args.args[0].include_fix_stage is False


@pytest.mark.asyncio
async def test_tool_run_rejects_unknown_command(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")

    with pytest.raises(_ToolError, match="コマンドが見つかりません"):
        await pyfltr.cli.mcp_run_tools.tool_run(paths=[str(target)], commands=["unknown-command"])


@pytest.mark.asyncio
async def test_tool_run_changed_since_uses_work_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,
) -> None:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    target = work_dir / "sample.txt"
    target.write_text("hello\n", encoding="utf-8")
    server_cwd = tmp_path / "server"
    server_cwd.mkdir()
    monkeypatch.chdir(server_cwd)
    filter_changed = mocker.patch(
        "pyfltr.command.targets.filter_by_changed_since",
        return_value=[target],
    )
    mocker.patch("pyfltr.cli.pipeline.run_commands_with_cli", return_value=[])

    await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt"],
        commands=["ec"],
        changed_since="HEAD",
        work_dir=str(work_dir),
    )

    assert filter_changed.call_args.kwargs["cwd"] == work_dir.resolve()


@pytest.mark.asyncio
async def test_tool_run_scans_and_deduplicates_from_work_dir(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker,
) -> None:
    work_dir = tmp_path / "project"
    source_dir = work_dir / "src"
    source_dir.mkdir(parents=True)
    included = source_dir / "included.txt"
    included.write_text("included\n", encoding="utf-8")
    excluded = source_dir / "excluded.txt"
    excluded.write_text("excluded\n", encoding="utf-8")
    (work_dir / "src_alias").symlink_to("src", target_is_directory=True)
    (work_dir / "pyproject.toml").write_text(
        '[tool.pyfltr]\nec = true\nextend-exclude = ["*/excluded.txt"]\n',
        encoding="utf-8",
    )
    server_cwd = tmp_path / "server"
    server_cwd.mkdir()
    monkeypatch.chdir(server_cwd)
    scanned: list[pathlib.Path] = []

    def capture_files(
        _commands: list[str],
        _args: argparse.Namespace,
        base_ctx: typing.Any,
        **_kwargs: typing.Any,
    ) -> list[typing.Any]:
        scanned.extend(base_ctx.all_files)
        return []

    mocker.patch("pyfltr.cli.pipeline.run_commands_with_cli", side_effect=capture_files)

    await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["src", "src_alias"],
        commands=["ec"],
        work_dir=str(work_dir),
        no_gitignore=True,
    )

    assert scanned == [pathlib.Path("src/included.txt")]


@pytest.mark.asyncio
@pytest.mark.skipif(not shutil.which("typos"), reason="typos コマンドが環境にない")
async def test_tool_run_with_typos(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("hello world\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(sample)],
        commands=["typos"],
    )

    assert result.run_id is not None
    assert len(result.run_id) > 0
    assert isinstance(result.exit_code, int)
    assert isinstance(result.failed, list)
    assert isinstance(result.commands, list)


@pytest.mark.asyncio
async def test_tool_run_keeps_stdout_clean_and_text_on_stderr(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    await pyfltr.cli.mcp_run_tools.tool_run(paths=[str(sample)], commands=["ec"])

    captured = capsys.readouterr()
    assert captured.out == "", f"stdout に漏れている: {captured.out!r}"
    assert "----- pyfltr" in captured.err


@pytest.mark.asyncio
async def test_tool_run_returns_run_id(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "input.txt"
    sample.write_text("This is a simple test file.\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(sample)],
        commands=["ec"],
    )

    assert result.run_id is not None
    assert len(result.run_id) == 26
    assert isinstance(result.exit_code, int)

    store = pyfltr.state.archive.ArchiveStore()
    summaries = store.list_runs(limit=1)
    assert len(summaries) == 1
    assert summaries[0].run_id == result.run_id


@pytest.mark.asyncio
async def test_tool_run_reports_disabled_command(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_run_tools.tool_run(paths=[str(sample)], commands=["ec"], disable=["ec"])

    assert result.skipped_reason is not None
    assert "ec" in result.skipped_reason
    assert "--enable=ec" in result.skipped_reason
    assert not result.commands
    assert result.completion == "not_reached"
    assert result.incomplete_commands == ["ec"]
    assert not result.completed_commands


@pytest.mark.asyncio
async def test_tool_run_returns_warnings_without_stderr_duplication(
    tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture
) -> None:
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    with caplog.at_level("WARNING"):
        result = await pyfltr.cli.mcp_run_tools.tool_run(paths=[str(sample)], commands=["ec"], disable=["ec"])

    commands_warnings = [warning for warning in result.warnings if warning.source == "commands"]
    assert len(commands_warnings) == 1
    assert "ec" in commands_warnings[0].message
    assert commands_warnings[0].hint is not None
    assert "--enable=ec" in commands_warnings[0].hint
    assert not [record for record in caplog.records if commands_warnings[0].message in record.getMessage()]


@pytest.mark.asyncio
async def test_tool_run_returns_guidance_on_failure(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    (work_dir / "sample.txt").write_text("value\n", encoding="utf-8")
    (work_dir / "pyproject.toml").write_text(
        "[tool.pyfltr]\nrespect-gitignore = false\n\n"
        "[tool.pyfltr.custom-commands.always-fail]\n"
        f"path = {json.dumps(sys.executable)}\n"
        'args = ["-c", "import sys; sys.exit(1)"]\n'
        'type = "linter"\ntargets = ["*.txt"]\npass-filenames = false\n',
        encoding="utf-8",
    )

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt"], commands=["always-fail"], work_dir=str(work_dir), no_cache=True
    )

    assert result.failed == ["always-fail"]
    assert any("only_failed=True" in item for item in result.guidance)
    assert any(result.run_id is not None and result.run_id in item for item in result.guidance)


@pytest.mark.asyncio
async def test_tool_run_does_not_carry_over_skipped_reason(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    skipped = await pyfltr.cli.mcp_run_tools.tool_run(paths=[str(sample)], commands=["ec"], disable=["ec"])
    assert skipped.skipped_reason is not None

    result = await pyfltr.cli.mcp_run_tools.tool_run(paths=[str(sample)], commands=["ec"])
    assert result.skipped_reason is None


@pytest.mark.asyncio
async def test_tool_run_retry_command_uses_cli_arguments(tmp_path: pathlib.Path, mocker) -> None:
    sample = tmp_path / "failure.txt"
    sample.write_text("failure\n", encoding="utf-8")
    failed = _make_result(
        "ec",
        returncode=1,
        archived=False,
        target_files=[sample],
    )

    def fake_run_commands(*_args: typing.Any, **kwargs: typing.Any) -> list[typing.Any]:
        kwargs["archive_hook"](failed)
        kwargs["on_result"](failed)
        return [failed]

    mocker.patch("pyfltr.cli.pipeline.run_commands_with_cli", side_effect=fake_run_commands)

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(sample)],
        mode="ci",
        commands=["ec"],
        no_fix=True,
    )

    assert result.failed == ["ec"]
    assert set(result.retry_commands) == {"ec"}
    retry_command = result.retry_commands["ec"]
    assert " ci " in retry_command
    assert "--commands=ec" in retry_command
    assert "--no-fix" in retry_command
    assert " mcp " not in retry_command


@pytest.mark.asyncio
async def test_tool_run_retry_commands_includes_failed(tmp_path: pathlib.Path, mocker) -> None:
    bad_py = tmp_path / "bad.py"
    bad_py.write_text("import os\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nruff-check = true\n", encoding="utf-8")

    proc = subprocess.CompletedProcess(["ruff"], returncode=1, stdout="[]", stderr="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(bad_py)],
        work_dir=str(tmp_path),
        mode="ci",
        commands=["ruff-check"],
    )

    assert result.failed == ["ruff-check"]
    assert set(result.retry_commands) == {"ruff-check"}
    assert "--commands=ruff-check" in result.retry_commands["ruff-check"]


@pytest.mark.asyncio
async def test_tool_run_from_run_without_only_failed_raises() -> None:
    with pytest.raises(_ToolError, match="only_failed"):
        await pyfltr.cli.mcp_run_tools.tool_run(
            paths=["dummy"],
            from_run="latest",
        )


@pytest.mark.asyncio
async def test_tool_run_only_failed_no_previous_run(tmp_path: pathlib.Path) -> None:
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(sample)],
        commands=["ec"],
        only_failed=True,
    )

    assert result.run_id is None
    assert result.exit_code == 0
    assert not result.failed
    assert not result.commands
    assert result.skipped_reason is not None
    assert "直前の run が無い" in result.skipped_reason
    assert "only_failed=False" in result.skipped_reason
    assert [warning.source for warning in result.warnings] == ["only-failed"]
    assert result.completion == "not_reached"
    assert result.files_reached == 0
    assert not result.completed_commands


@pytest.mark.asyncio
async def test_tool_run_only_failed_unresolved_from_run(tmp_path: pathlib.Path) -> None:
    _seed_run(tmp_path)
    sample = tmp_path / "input.txt"
    sample.write_text("hello\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_run_tools.tool_run(paths=[str(sample)], commands=["ec"], only_failed=True, from_run="ZZZZZZ")

    assert result.run_id is None
    assert result.exit_code == 0
    assert result.skipped_reason is not None
    assert "from_run 'ZZZZZZ' を解決できない" in result.skipped_reason
    assert "直前の run が無い" not in result.skipped_reason


@pytest.mark.asyncio
async def test_tool_run_reports_subproject_toml_error_to_client(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[tool.pyfltr]\nrespect-gitignore = false\nsubproject-use-gitignore = false\n", encoding="utf-8"
    )
    child = project / "child"
    child.mkdir()
    invalid = child / "pyproject.toml"
    invalid.write_text("[tool.pyfltr\n", encoding="utf-8")
    (child / "sample.py").write_text("value = 1\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(child / "sample.py")], commands=["mypy"], work_dir=str(project), no_cache=True
    )

    assert result.exit_code == 1
    assert result.completion == "not_reached"
    config_warnings = [warning for warning in result.warnings if warning.source == "config"]
    assert len(config_warnings) == 1
    assert str(invalid) in config_warnings[0].message
    assert "該当位置をTOMLの構文に直し" in config_warnings[0].hint
    assert "`run`ツール" in config_warnings[0].hint


@pytest.mark.asyncio
async def test_tool_run_only_failed_partial_exclusion_sets_skipped_reason(tmp_path: pathlib.Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("a.txt", "b.txt"):
        (project / name).write_text("value\n", encoding="utf-8")
    lines = ["[tool.pyfltr]", "respect-gitignore = false", ""]
    for name, failed_file in (("fail-a", "a.txt"), ("fail-b", "b.txt")):
        code = f"import sys; print({failed_file + ':1: bad'!r}); sys.exit(1)"
        lines.extend(
            [
                f"[tool.pyfltr.custom-commands.{name}]",
                f"path = {json.dumps(sys.executable)}",
                f"args = {json.dumps(['-c', code])}",
                'type = "linter"',
                'targets = ["*.txt"]',
                "pass-filenames = false",
                'error-pattern = "^(?P<file>[^:]+):(?P<line>\\\\d+): (?P<message>.+)$"',
                "",
            ]
        )
    (project / "pyproject.toml").write_text("\n".join(lines), encoding="utf-8")

    first = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["a.txt", "b.txt"], commands=["fail-a", "fail-b"], work_dir=str(project), no_cache=True
    )
    assert sorted(first.failed) == ["fail-a", "fail-b"]

    second = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["a.txt"], commands=["fail-a", "fail-b"], work_dir=str(project), no_cache=True, only_failed=True
    )

    assert second.run_id is not None
    assert [command.command for command in second.commands] == ["fail-a"]
    assert second.skipped_reason is not None
    assert "fail-b" in second.skipped_reason
    assert "交差しません" in second.skipped_reason


@pytest.mark.parametrize("include_disabled", [False, True])
@pytest.mark.asyncio
async def test_tool_run_archive_init_failure_preserves_results(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, include_disabled: bool
) -> None:
    project = tmp_path / "project"
    _write_custom_commands_project(project, {"always-fail": "import sys; sys.exit(1)"}, extra="textlint = false")

    def fail_start(*_args: typing.Any, **_kwargs: typing.Any) -> typing.Never:
        raise OSError("archive storage unavailable")

    monkeypatch.setattr(pyfltr.state.archive.ArchiveStore, "start_run", fail_start)
    commands = ["always-fail", "textlint"] if include_disabled else ["always-fail"]
    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt"], commands=commands, work_dir=str(project), no_cache=True
    )

    assert result.run_id is None
    assert result.exit_code == 1
    assert result.failed == ["always-fail"]
    assert [(command.command, command.status) for command in result.commands] == [("always-fail", "failed")]
    assert result.completed_commands == ["always-fail"]
    assert "always-fail" in result.retry_commands
    assert any("retry_commands" in item for item in result.guidance)
    guidance = " ".join(result.guidance)
    assert "show-run" not in guidance
    assert "show_run" not in guidance
    assert "only_failed" not in guidance
    assert "--only-failed" not in guidance
    assert "<run_id>" not in guidance
    archive_warning = next(warning for warning in result.warnings if warning.source == "archive")
    assert "archive storage unavailable" in archive_warning.message
    assert "--no-archive" not in archive_warning.hint
    assert "書き込み権限と空き容量" in archive_warning.hint
    assert "MCPのrunでは実行アーカイブを無効化できません" in archive_warning.hint
    if include_disabled:
        assert result.skipped_reason is not None
        commands_warning = next(warning for warning in result.warnings if warning.source == "commands")
        assert commands_warning.message in result.skipped_reason
        assert commands_warning.hint in result.skipped_reason
        assert "textlint" in result.skipped_reason
    else:
        assert result.skipped_reason is None


@pytest.mark.asyncio
async def test_tool_run_completion_completed(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    _write_custom_commands_project(work_dir, {"always-ok": "pass"})

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt"], commands=["always-ok"], work_dir=str(work_dir), no_cache=True
    )

    assert result.exit_code == 0
    assert result.completion == "completed"
    assert result.files_reached == 1
    assert result.completed_commands == ["always-ok"]
    assert not result.incomplete_commands
    assert not result.missing_targets
    assert not result.fully_excluded_files


@pytest.mark.asyncio
async def test_tool_run_completion_completed_with_failure(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    _write_custom_commands_project(work_dir, {"always-fail": "import sys; sys.exit(1)"})

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt"], commands=["always-fail"], work_dir=str(work_dir), no_cache=True
    )

    assert result.exit_code == 1
    assert result.failed == ["always-fail"]
    assert result.completion == "completed"
    assert result.completed_commands == ["always-fail"]


@pytest.mark.asyncio
async def test_tool_run_completion_partial_missing(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    _write_custom_commands_project(work_dir, {"always-ok": "pass"})

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt", "missing.txt"], commands=["always-ok"], work_dir=str(work_dir), no_cache=True
    )

    assert result.exit_code == 0
    assert result.completion == "incomplete"
    assert result.completed_commands == ["always-ok"]
    assert len(result.missing_targets) == 1
    assert result.missing_targets[0].endswith("missing.txt")


@pytest.mark.asyncio
async def test_tool_run_completion_early_exit_missing(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    _write_custom_commands_project(work_dir, {"always-ok": "pass"})

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["missing.txt"], commands=["always-ok"], work_dir=str(work_dir), no_cache=True
    )

    assert result.run_id is None
    assert result.exit_code == 1
    assert result.completion == "not_reached"
    assert result.files_reached == 0
    assert not result.completed_commands
    assert len(result.missing_targets) == 1


@pytest.mark.asyncio
async def test_tool_run_completion_timeout(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    _write_custom_commands_project(
        work_dir,
        {"always-ok": "pass", "too-slow": "import time; time.sleep(300)"},
        extra="command-timeout = 1",
    )

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt"], commands=["always-ok", "too-slow"], work_dir=str(work_dir), no_cache=True
    )

    assert result.completion == "incomplete"
    assert result.completed_commands == ["always-ok"]
    assert result.incomplete_commands == ["too-slow"]


@pytest.mark.parametrize("mode", ["run", "fast", "ci"])
@pytest.mark.asyncio
async def test_tool_run_completion_same_across_modes(tmp_path: pathlib.Path, mode: typing.Literal["run", "fast", "ci"]) -> None:
    work_dir = tmp_path / "project"
    _write_custom_commands_project(work_dir, {"always-ok": "pass"})

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt", "missing.txt"], mode=mode, commands=["always-ok"], work_dir=str(work_dir), no_cache=True
    )

    assert result.completion == "incomplete"
    assert result.files_reached == 1
    assert result.completed_commands == ["always-ok"]
    assert not result.incomplete_commands
    assert len(result.missing_targets) == 1


@pytest.mark.parametrize(
    ("commands", "expected", "expected_incomplete"),
    [
        (None, "completed", []),
        (["ec", "typos"], "incomplete", ["typos"]),
    ],
    ids=["コマンド未指定", "コマンド明示"],
)
@pytest.mark.asyncio
async def test_tool_run_completion_no_target_skip(
    tmp_path: pathlib.Path,
    mocker,
    commands: list[str] | None,
    expected: str,
    expected_incomplete: list[str],
) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text("hello\n", encoding="utf-8")
    evaluated = _make_result("ec", returncode=0, archived=False)
    no_target = _make_result("typos", returncode=None, files=0, archived=False)
    no_target.not_applicable = True

    def fake_run_commands(*_args: typing.Any, **kwargs: typing.Any) -> list[typing.Any]:
        for result in (evaluated, no_target):
            kwargs["on_result"](result)
        return [evaluated, no_target]

    mocker.patch("pyfltr.cli.pipeline.run_commands_with_cli", side_effect=fake_run_commands)

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(sample)], commands=commands, enable=["ec", "typos"], no_cache=True
    )

    assert result.completion == expected
    assert result.completed_commands == ["ec"]
    assert result.incomplete_commands == expected_incomplete


@pytest.mark.asyncio
async def test_tool_run_completion_matches_cli_jsonl_summary(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    _write_custom_commands_project(work_dir, {"always-ok": "pass", "disabled-check": "pass"})
    destination = tmp_path / "out.jsonl"

    mcp_result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt", "missing.txt"],
        commands=["always-ok", "disabled-check"],
        disable=["disabled-check"],
        work_dir=str(work_dir),
        no_cache=True,
    )
    cli_exit = pyfltr.cli.main.run(
        [
            "run-for-agent",
            "--work-dir",
            str(work_dir),
            f"--output-file={destination}",
            "--commands=always-ok,disabled-check",
            "--disable=disabled-check",
            "--no-cache",
            str(work_dir / "sample.txt"),
            str(work_dir / "missing.txt"),
        ]
    )

    records = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines() if line.strip()]
    summary = records[-1]
    assert summary["kind"] == "summary"
    assert not [record for record in records if record["kind"] == "command"]
    assert cli_exit == mcp_result.exit_code
    assert summary["completion"] == mcp_result.completion == "incomplete"
    assert summary["files_reached"] == mcp_result.files_reached == 1
    assert summary["completed_commands"] == mcp_result.completed_commands == ["always-ok"]
    assert summary["incomplete_commands"] == mcp_result.incomplete_commands == ["disabled-check"]


@pytest.mark.asyncio
async def test_tool_run_propagates_work_dir_to_command_archive_and_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    helper = work_dir / "cwd_helper.py"
    helper.write_text("import pathlib\nprint(pathlib.Path.cwd())\n", encoding="utf-8")
    target = work_dir / "sample.txt"
    target.write_text("value\n", encoding="utf-8")
    (work_dir / "pyproject.toml").write_text(
        "[tool.pyfltr]\nrespect-gitignore = false\n\n"
        "[tool.pyfltr.custom-commands.cwd-check]\n"
        f"path = {json.dumps(sys.executable)}\n"
        f"args = [{json.dumps(str(helper))}]\n"
        'type = "linter"\ntargets = ["*.txt"]\npass-filenames = false\n',
        encoding="utf-8",
    )
    server_cwd = tmp_path / "server"
    server_cwd.mkdir()
    monkeypatch.chdir(server_cwd)

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["sample.txt"],
        commands=["cwd-check"],
        work_dir=str(work_dir),
        no_cache=True,
    )

    assert result.exit_code == 0
    assert result.run_id is not None
    store = pyfltr.state.archive.ArchiveStore()
    assert store.read_meta(result.run_id)["cwd"] == str(work_dir)
    assert store.read_tool_output(result.run_id, "cwd-check") == str(work_dir)
    assert f"cwd:            {work_dir}" in capsys.readouterr().err
    assert pathlib.Path.cwd() == server_cwd


def test_tool_run_parallel_work_dirs_do_not_interfere(tmp_path: pathlib.Path) -> None:
    server_cwd = pathlib.Path.cwd()
    work_dirs = [tmp_path / "first", tmp_path / "second"]
    for work_dir in work_dirs:
        work_dir.mkdir()
        helper = work_dir / "cwd_helper.py"
        helper.write_text(
            "import pathlib\npathlib.Path('cwd.txt').write_text(str(pathlib.Path.cwd()), encoding='utf-8')\n",
            encoding="utf-8",
        )
        (work_dir / "sample.txt").write_text("value\n", encoding="utf-8")
        (work_dir / "pyproject.toml").write_text(
            "[tool.pyfltr]\nrespect-gitignore = false\n\n"
            "[tool.pyfltr.custom-commands.cwd-check]\n"
            f"path = {json.dumps(sys.executable)}\n"
            f"args = [{json.dumps(str(helper))}]\n"
            'type = "linter"\ntargets = ["*.txt"]\npass-filenames = false\n',
            encoding="utf-8",
        )

    def _run(work_dir: pathlib.Path) -> pyfltr.cli.mcp_models.RunResult:
        return asyncio.run(
            pyfltr.cli.mcp_run_tools.tool_run(
                paths=["sample.txt"],
                commands=["cwd-check"],
                work_dir=str(work_dir),
                no_cache=True,
            )
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_run, work_dirs))

    assert [result.exit_code for result in results] == [0, 0]
    assert [(work_dir / "cwd.txt").read_text(encoding="utf-8") for work_dir in work_dirs] == [
        str(work_dir) for work_dir in work_dirs
    ]
    assert pathlib.Path.cwd() == server_cwd


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("pnpm") is None, reason="Linux上のpnpmが必要")
@pytest.mark.asyncio
async def test_tool_run_pnpm_prettier_uses_work_dir(tmp_path: pathlib.Path) -> None:
    work_dir = tmp_path / "project"
    shim = work_dir / "node_modules" / ".bin" / "prettier"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | 0o111)
    (work_dir / "package.json").write_text('{"name": "cwd-test", "private": true}\n', encoding="utf-8")
    (work_dir / "pyproject.toml").write_text(
        '[tool.pyfltr]\nprettier = true\njs-runner = "pnpm"\nrespect-gitignore = false\n',
        encoding="utf-8",
    )

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=["package.json"],
        commands=["prettier"],
        work_dir=str(work_dir),
        no_fix=True,
        no_cache=True,
    )

    assert result.exit_code == 0
    assert not result.failed


def _write_custom_commands_project(work_dir: pathlib.Path, scripts: dict[str, str], *, extra: str = "") -> None:
    work_dir.mkdir()
    (work_dir / "sample.txt").write_text("value\n", encoding="utf-8")
    lines = ["[tool.pyfltr]", "respect-gitignore = false", extra, ""]
    for name, code in scripts.items():
        lines.extend(
            [
                f"[tool.pyfltr.custom-commands.{name}]",
                f"path = {json.dumps(sys.executable)}",
                f"args = {json.dumps(['-c', code])}",
                'type = "linter"',
                'targets = ["*.txt"]',
                "pass-filenames = false",
                "fast = true",
                "",
            ]
        )
    (work_dir / "pyproject.toml").write_text("\n".join(lines), encoding="utf-8")
