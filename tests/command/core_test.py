import pathlib

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_


def test_command_result_cached_defaults() -> None:
    """`CommandResult`の新フィールドcached/cached_fromの既定値テスト。"""
    result = pyfltr.command.core_.CommandResult(
        command="mypy",
        command_type="linter",
        commandline=["mypy"],
        returncode=0,
        formatter_failed=False,
        files=1,
        output="",
        elapsed=0.1,
    )
    assert result.cached is False
    assert result.cached_from is None


def test_command_result_merge_retry_count() -> None:
    """`CommandResult.merge()`でretry_countが合計される。"""

    def _make_result(retry_count: int) -> pyfltr.command.core_.CommandResult:
        return pyfltr.command.core_.CommandResult(
            command="mypy",
            command_type="linter",
            commandline=["mypy"],
            returncode=0,
            formatter_failed=False,
            files=1,
            output="",
            elapsed=0.1,
            retry_count=retry_count,
        )

    r1 = _make_result(retry_count=1)
    r2 = _make_result(retry_count=2)
    merged = pyfltr.command.core_.CommandResult.merge([r1, r2])
    assert merged.retry_count == 3


def test_merge_limits_slow_tests_across_subprojects() -> None:
    """複数結果の遅いテストを秒数降順の上位件数へ正規化する。"""

    def _make_result(seconds: list[float]) -> pyfltr.command.core_.CommandResult:
        return pyfltr.command.core_.CommandResult(
            command="pytest",
            command_type="tester",
            commandline=["pytest"],
            returncode=0,
            formatter_failed=False,
            files=1,
            output="",
            elapsed=0.1,
            slow_tests=[pyfltr.command.slow_tests.SlowTest(f"tests/test_{second}.py", "call", second) for second in seconds],
        )

    merged = pyfltr.command.core_.CommandResult.merge([_make_result([1.0, 6.0, 3.0]), _make_result([2.0, 5.0, 4.0])])
    assert [test.seconds for test in merged.slow_tests] == [6.0, 5.0, 4.0, 3.0, 2.0]


def _make_merge_input(returncode: int | None) -> pyfltr.command.core_.CommandResult:
    """`merge`へ渡すサブプロジェクト別の結果を生成する。

    `returncode=None`は対象0件で起動しなかった結果（`dispatcher`の早期返却）を模し、
    起動コマンドとrunner情報を持たない。それ以外は実行済みの結果として値を持つ。
    """
    executed = returncode is not None
    return pyfltr.command.core_.CommandResult(
        command="ruff-check",
        command_type="linter",
        commandline=["uv", "run", "ruff", "check"] if executed else [],
        returncode=returncode,
        formatter_failed=False,
        files=1 if executed else 0,
        output="",
        elapsed=0.1,
        effective_runner="uv" if executed else None,
        runner_source="python-runner" if executed else None,
        runner_fallback="uvx" if executed else None,
    )


@pytest.mark.parametrize(
    ("returncodes", "expected_status", "expected_returncode"),
    [
        ([None, 0], "succeeded", 0),
        ([0, None], "succeeded", 0),
        ([None, 1], "failed", 1),
        ([None, None], "skipped", None),
    ],
)
def test_merge_status_prefers_executed_result(
    returncodes: list[int | None], expected_status: str, expected_returncode: int | None
) -> None:
    """先頭が対象0件のskippedでも、実行済みの結果が集約後の`status`へ反映される。"""
    merged = pyfltr.command.core_.CommandResult.merge([_make_merge_input(rc) for rc in returncodes])
    assert merged.status == expected_status
    assert merged.returncode == expected_returncode


@pytest.mark.parametrize("executed_returncode", [0, 1])
def test_merge_inherits_commandline_from_executed_result(executed_returncode: int) -> None:
    """先頭が対象0件のskippedでも、起動コマンドとrunner情報は実行済みの結果から引き継ぐ。"""
    merged = pyfltr.command.core_.CommandResult.merge([_make_merge_input(None), _make_merge_input(executed_returncode)])
    assert merged.commandline == ["uv", "run", "ruff", "check"]
    assert merged.effective_runner == "uv"
    assert merged.runner_source == "python-runner"
    assert merged.runner_fallback == "uvx"


def test_execution_context_resolves_effective_cwd_and_workspace_root(tmp_path: pathlib.Path) -> None:
    """実効cwdとuv workspace rootをサブプロジェクト情報から導出する。"""
    start_cwd = tmp_path / "repo"
    workspace_root = start_cwd
    subproject_cwd = start_cwd / "packages" / "app"
    other_cwd = start_cwd / "packages" / "other"
    subprojects = [
        pyfltr.command.subprojects.Subproject(
            cwd=subproject_cwd,
            relative="packages/app",
            uv_workspace_root=workspace_root,
        ),
        pyfltr.command.subprojects.Subproject(cwd=other_cwd, relative="packages/other"),
    ]
    config = pyfltr.config.config.create_default_config()
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subprojects=subprojects,
    )

    root_context = pyfltr.command.core_.ExecutionContext(base=base)
    subproject_context = pyfltr.command.core_.ExecutionContext(base=base, subproject_cwd=subproject_cwd)

    assert root_context.effective_cwd == start_cwd
    assert root_context.uv_workspace_root is None
    assert subproject_context.effective_cwd == subproject_cwd
    assert subproject_context.uv_workspace_root == workspace_root
