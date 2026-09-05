"""CommandResult.statusと成否を消費する各経路の整合性テスト。"""

import dataclasses
import json
import pathlib
import subprocess
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_models
import pyfltr.cli.mcp_server
import pyfltr.cli.pipeline
import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.output.jsonl
import pyfltr.output.sarif
import pyfltr.state.archive
import pyfltr.state.retry
from tests.conftest import make_args as _make_args
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_execution_context as _make_ctx


@pytest.fixture(name="plain_linter_project")
def _plain_linter_project(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """plain実行経路を通るカスタムlinterの検証用プロジェクトを作成する。"""
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
    """plain-linterだけが非ゼロ終了する外部プロセス境界を再現する。"""

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
    """成否消費側へ渡る入力状態ごとの結果を固定する。"""
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
        original_cwd=".",
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
    """plain linterの失敗をJSONLと実行アーカイブへ反映する。"""
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
) -> None:
    """plain linterの失敗がCLIのfail-fast処理を起動する。"""
    del plain_linter_subprocess
    work_dir, target = plain_linter_project
    config = pyfltr.config.config.load_config(config_dir=work_dir)

    results = pyfltr.cli.pipeline.run_commands_with_cli(
        ["plain-linter", "follow-up"],
        _make_args(),
        _make_ctx(config, [target], start_cwd=work_dir).base,
        per_command_log=False,
        fail_fast=True,
    )

    statuses = {result.command: result.status for result in results}
    assert statuses == {"plain-linter": "failed", "follow-up": "skipped"}


def test_plain_linter_failure_sarif_execution_unsuccessful(
    plain_linter_project: tuple[pathlib.Path, pathlib.Path],
    plain_linter_subprocess: typing.Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """plain linterの失敗をSARIFの実行失敗へ反映する。"""
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
    """plain linterの失敗をMCPのfailed一覧へ反映する。"""
    del plain_linter_subprocess
    work_dir, target = plain_linter_project
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(work_dir / "cache"))

    result = await pyfltr.cli.mcp_server.tool_run_for_agent(
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
    """formatterの書き換えは成功のまま再実行対象にしない。"""
    result = _make_result("ruff-format", command_type="formatter", returncode=1)
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=["ci", "--commands", ""],
        launcher_prefix=["pyfltr"],
        original_cwd=".",
    )

    assert result.status == "formatted"
    assert not result.failed
    assert not result.needs_rerun
    assert result.retry_command is None


def test_warning_severity_emits_retry_command_without_failing() -> None:
    """warningへ格下げした失敗は再実行対象だがパイプラインを失敗させない。"""
    result = dataclasses.replace(_make_result("mypy", returncode=1), severity="warning")
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=["ci", "--commands", ""],
        launcher_prefix=["pyfltr"],
        original_cwd=".",
    )

    assert result.status == "warning"
    assert not result.failed
    assert result.needs_rerun
    assert result.retry_command is not None
    assert pyfltr.cli.pipeline.calculate_returncode([result], False) == 0


def test_tool_meta_keys_match_expected_set(tmp_path: pathlib.Path) -> None:
    """実行アーカイブの必須メタ情報キーを完全一致で固定する。"""
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
    """MCPのコマンドモデルが公開するフィールド集合を完全一致で固定する。"""
    assert set(pyfltr.cli.mcp_models.CommandSummaryModel.model_fields) == {
        "command",
        "status",
        "diagnostics",
        "elapsed",
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
    }
