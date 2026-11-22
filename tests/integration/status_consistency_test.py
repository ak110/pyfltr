import dataclasses
import json
import pathlib
import subprocess
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_models
import pyfltr.cli.mcp_run_tools
import pyfltr.cli.pipeline
import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.output.sarif
import pyfltr.state.archive
import pyfltr.state.retry
from tests.conftest import StageExecutorControl
from tests.conftest import make_args as _make_args
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_execution_context as _make_ctx


@pytest.fixture(name="plain_linter_project")
def _plain_linter_project(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyfltr]
jobs = 1

[tool.pyfltr.custom-commands.plain-linter]
type = "linter"
path = "plain-linter"
targets = ["*.txt"]

[tool.pyfltr.custom-commands.follow-up]
type = "linter"
path = "follow-up"
targets = ["*.txt"]
""".lstrip(),
        encoding="utf-8",
    )
    target = tmp_path / "input.txt"
    target.write_text("invalid\n", encoding="utf-8")
    return tmp_path, target


@pytest.fixture(name="plain_linter_subprocess")
def _plain_linter_subprocess(mocker: typing.Any) -> typing.Any:

    def _run(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            commandline,
            returncode=1 if commandline[0] == "plain-linter" else 0,
            stdout="plain failure" if commandline[0] == "plain-linter" else "",
        )

    return mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_run)


@pytest.mark.parametrize(
    (
        "case",
        "command_type",
        "returncode",
        "severity",
        "formatter_failed",
        "resolution_failed",
        "timeout_exceeded",
        "cached",
        "expected_status",
        "expected_failed",
        "expected_rerun",
        "expected_exit",
    ),
    [
        ("linter-error", "linter", 1, "error", False, False, False, False, "failed", True, True, 1),
        ("tester-warning", "tester", 1, "warning", False, False, False, False, "warning", False, True, 0),
        ("linter-success", "linter", 0, "error", False, False, False, False, "succeeded", False, False, 0),
        ("formatter-change", "formatter", 1, "error", False, False, False, False, "formatted", False, False, 1),
        ("formatter-failure", "formatter", 1, "error", True, False, False, False, "failed", True, True, 1),
        ("resolution-failure", "linter", 127, "error", False, True, False, False, "resolution_failed", True, True, 1),
        ("timeout", "formatter", 1, "error", False, False, True, False, "failed", True, True, 1),
        ("skipped", "linter", None, "error", False, False, False, False, "skipped", False, False, 0),
        ("cached", "linter", 0, "error", False, False, False, True, "succeeded", False, False, 0),
    ],
)
def test_status_consumers_are_consistent(
    case: str,
    command_type: str,
    returncode: int | None,
    severity: str,
    formatter_failed: bool,
    resolution_failed: bool,
    timeout_exceeded: bool,
    cached: bool,
    expected_status: str,
    expected_failed: bool,
    expected_rerun: bool,
    expected_exit: int,
) -> None:
    result = _make_result(
        case,
        command_type=command_type,
        returncode=returncode,
        formatter_failed=formatter_failed,
        resolution_failed=resolution_failed,
        cached=cached,
    )
    result = dataclasses.replace(result, severity=severity, timeout_exceeded=timeout_exceeded)

    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=["ci", "--commands", ""],
        launcher_prefix=["pyfltr"],
        target_base_cwd=".",
    )
    sarif = pyfltr.output.sarif.build_sarif(
        [result],
        pyfltr.config.config.create_default_config(),
        exit_code=expected_exit,
    )

    assert result.status == expected_status
    assert result.failed is expected_failed
    assert result.needs_rerun is expected_rerun
    assert (result.retry_command is not None) is (expected_rerun and not cached)
    assert pyfltr.cli.pipeline.calculate_returncode([result], False) == expected_exit
    assert sarif["runs"][0]["invocations"][0]["executionSuccessful"] is (not expected_failed)
    assert pyfltr.command.core_.is_failed_status(result.status) is expected_failed


def test_plain_linter_failure_emits_retry_command_and_archive(
    plain_linter_project: tuple[pathlib.Path, pathlib.Path],
    plain_linter_subprocess: typing.Any,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del plain_linter_subprocess
    work_dir, target = plain_linter_project
    cache_root = work_dir / "cache"
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(cache_root))

    exit_code = pyfltr.cli.main.run(
        [
            "run",
            "--work-dir",
            str(work_dir),
            "--commands=plain-linter",
            "--output-format=jsonl",
            "--no-fix",
            "--no-cache",
            str(target),
        ]
    )

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    header = next(record for record in records if record["kind"] == "header")
    command = next(record for record in records if record["kind"] == "command")
    archived = pyfltr.state.archive.ArchiveStore(cache_root=cache_root).read_tool_meta(header["run_id"], "plain-linter")

    assert exit_code == 1
    assert command["status"] == "failed"
    assert command["retry_command"]
    assert archived["status"] == "failed"
    assert archived["retry_command"] == command["retry_command"]


def test_plain_linter_failure_triggers_fail_fast(
    plain_linter_project: tuple[pathlib.Path, pathlib.Path],
    plain_linter_subprocess: typing.Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del plain_linter_subprocess
    work_dir, target = plain_linter_project
    config = pyfltr.config.config.load_config(config_dir=work_dir)
    control = StageExecutorControl(run_on_submit={"plain-linter"})
    control.install(monkeypatch)

    results = pyfltr.cli.pipeline.run_commands_with_cli(
        ["plain-linter", "follow-up"],
        _make_args(),
        _make_ctx(config, [target], start_cwd=work_dir).base,
        per_command_log=False,
        fail_fast=True,
    )

    statuses = {result.command: result.status for result in results}
    assert statuses == {"plain-linter": "failed", "follow-up": "skipped"}
    assert control.executed == ["plain-linter"]


def test_plain_linter_failure_sarif_execution_unsuccessful(
    plain_linter_project: tuple[pathlib.Path, pathlib.Path],
    plain_linter_subprocess: typing.Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    del plain_linter_subprocess
    work_dir, target = plain_linter_project

    exit_code = pyfltr.cli.main.run(
        [
            "run",
            "--work-dir",
            str(work_dir),
            "--commands=plain-linter",
            "--output-format=sarif",
            "--no-fix",
            "--no-cache",
            "--no-archive",
            str(target),
        ]
    )
    sarif = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert sarif["runs"][0]["invocations"][0]["executionSuccessful"] is False


@pytest.mark.asyncio
async def test_plain_linter_failure_listed_in_mcp_failed(
    plain_linter_project: tuple[pathlib.Path, pathlib.Path],
    plain_linter_subprocess: typing.Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del plain_linter_subprocess
    work_dir, target = plain_linter_project
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(work_dir / "cache"))

    result = await pyfltr.cli.mcp_run_tools.tool_run(
        paths=[str(target)],
        mode="ci",
        commands=["plain-linter"],
        no_fix=True,
        work_dir=str(work_dir),
    )

    assert result.failed == ["plain-linter"]
    assert result.commands[0].status == "failed"
    assert result.retry_commands


def test_formatted_result_keeps_no_rerun() -> None:
    result = _make_result("ruff-format", command_type="formatter", returncode=1)
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=["ci", "--commands", ""],
        launcher_prefix=["pyfltr"],
        target_base_cwd=".",
    )

    assert result.status == "formatted"
    assert not result.failed
    assert not result.needs_rerun
    assert result.retry_command is None


def test_warning_severity_emits_retry_command_without_failing() -> None:
    result = dataclasses.replace(_make_result("mypy", returncode=1), severity="warning")
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=["ci", "--commands", ""],
        launcher_prefix=["pyfltr"],
        target_base_cwd=".",
    )

    assert result.status == "warning"
    assert not result.failed
    assert result.needs_rerun
    assert result.retry_command is not None
    assert pyfltr.cli.pipeline.calculate_returncode([result], False) == 0


def test_tool_meta_keys_match_expected_set(tmp_path: pathlib.Path) -> None:
    store = pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)
    run_id = store.start_run(commands=["mypy"])
    store.write_tool_result(run_id, _make_result("mypy", returncode=1))

    assert set(store.read_tool_meta(run_id, "mypy")) == {
        "command",
        "type",
        "status",
        "returncode",
        "files",
        "elapsed",
        "diagnostics",
        "commandline",
    }


def test_mcp_command_models_field_sets() -> None:
    assert set(pyfltr.cli.mcp_models.CommandSummaryModel.model_fields) == {
        "command",
        "status",
        "diagnostics",
        "elapsed",
        "started_at",
        "finished_at",
        "launch_boundary_at",
        "slow_tests",
    }
    assert set(pyfltr.cli.mcp_models.CommandMetaModel.model_fields) == {
        "command",
        "type",
        "status",
        "returncode",
        "files",
        "elapsed",
        "diagnostics",
        "slow_tests",
        "retry_command",
        "started_at",
        "finished_at",
        "launch_boundary_at",
    }


def test_fix_stage_failure_reaches_exit_code_and_archive(
    tmp_path: pathlib.Path, mocker: typing.Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.pyfltr]
jobs = 1

[tool.pyfltr.custom-commands.fixable]
type = "linter"
path = "fixable"
targets = ["*.txt"]
fix-args = ["--fix"]
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / "input.txt").write_text("x\n", encoding="utf-8")
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(cache_root))

    def _run(commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        if "--fix" in commandline:
            return subprocess.CompletedProcess(commandline, returncode=1, stdout="fix exception")
        return subprocess.CompletedProcess(commandline, returncode=0, stdout="lint ok")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_run)

    exit_code = pyfltr.cli.main.run(
        ["run", "--work-dir", str(tmp_path), "--commands=fixable", "--no-cache", "--no-gitignore", "--output-format=jsonl"]
    )

    assert exit_code == 1
    store = pyfltr.state.archive.ArchiveStore(cache_root=cache_root)
    run_id = store.list_runs(limit=1)[0].run_id
    assert store.read_tool_meta(run_id, "fixable")["status"] == "failed"
    assert "fix exception" in store.read_tool_output(run_id, "fixable")
