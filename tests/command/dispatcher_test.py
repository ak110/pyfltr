import dataclasses
import json
import os
import pathlib
import subprocess
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.pytest_impact
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.tool_version
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
from tests import conftest as _testconf


def test_execute_command_direct_missing_returns_failed_result(tmp_path: pathlib.Path) -> None:
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
        assert result.failed is True
        assert "node_modules" in result.output
        assert "pnpm install" in result.output
        assert "pnpx" in result.output
    finally:
        os.chdir(original_cwd)


def test_execute_command_python_tool_missing_emits_runner_guidance(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")

    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True
    config.values["mypy-runner"] = "direct"

    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uv_available", lambda: False)
    monkeypatch.setattr("pyfltr.command.runner.ensure_uvx_available", lambda: False)
    monkeypatch.setattr("pyfltr.command.runner.cwd_has_uv_lock", lambda *_args: False)

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        result = pyfltr.command.dispatcher.execute_command(
            "mypy", _testconf.make_args(), _testconf.make_execution_context(config, [target])
        )
        assert result.status == "resolution_failed"
        assert "PATH 上にありません" in result.output
        assert "mypy-runner" in result.output
        assert "mypy-path" in result.output
    finally:
        os.chdir(original_cwd)


@pytest.mark.parametrize("command", ["semgrep", "sqlfluff"])
def test_uvx_default_tool_missing_returns_resolution_failed_with_individual_package_guidance(
    command: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = ".sql" if command == "sqlfluff" else ".py"
    target = tmp_path / f"sample{suffix}"
    target.write_text("SELECT 1;\n" if command == "sqlfluff" else "x = 1\n")
    config = pyfltr.config.config.create_default_config()
    config.values[command] = True
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uvx_available", lambda: False)
    monkeypatch.setattr("pyfltr.command.runner.shutil.which", lambda _name: None)

    result = pyfltr.command.dispatcher.execute_command(
        command,
        _testconf.make_args(allow_external_paths=True),
        _testconf.make_execution_context(config, [target]),
    )

    assert result.status == "resolution_failed"
    assert f"uv add --dev {command}" in result.output
    assert f'{command}-runner = "direct"' in result.output
    assert "pyfltr[python]" not in result.output
    assert f'{command}-runner = "uvx"' not in result.output


def test_execute_command_mise_not_registered_emits_switch_guidance(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "a.py"
    target.write_text("x = 1\n")

    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True
    config.values["mypy-runner"] = "mise"

    original_cwd = pathlib.Path.cwd()
    try:
        os.chdir(tmp_path)
        result = pyfltr.command.dispatcher.execute_command(
            "mypy", _testconf.make_args(), _testconf.make_execution_context(config, [target])
        )
        assert result.status == "resolution_failed"
        assert "miseバックエンド" in result.output
        assert "mypy-runner" in result.output
    finally:
        os.chdir(original_cwd)


def test_uv_audit_below_minimum_version_is_not_downgraded_by_severity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    pyfltr.command.tool_version._get_tool_version.cache_clear()  # pylint: disable=protected-access  # テスト間の隔離
    target = tmp_path / "pyproject.toml"
    target.write_text("[project]\nname = 'example'\n")

    def _fake_which(name: str, **kwargs: typing.Any) -> str:
        del kwargs
        return f"/usr/bin/{name}"

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="uv 0.10.9", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.runner.shutil, "which", _fake_which)
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    config = pyfltr.config.config.create_default_config()
    config.values["uv-audit"] = True
    config.values["uv-audit-severity"] = "warning"
    result = pyfltr.command.dispatcher.execute_command(
        "uv-audit", _testconf.make_args(allow_external_paths=True), _testconf.make_execution_context(config, [target])
    )
    assert result.status == "resolution_failed"


def test_auto_args_included_in_commandline(mocker, tmp_path: pathlib.Path) -> None:
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


def test_execute_command_propagates_severity_to_result(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    proc = subprocess.CompletedProcess(["pylint"], returncode=1, stdout="some lint failure")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["pylint"] = True
    config.values["pylint-severity"] = "warning"
    result = pyfltr.command.dispatcher.execute_command(
        "pylint", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )
    assert result.severity == "warning"
    assert result.status == "warning"


def test_execute_command_resolves_arid_paths_from_subproject_cwd(
    mocker, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.chdir(tmp_path)
    subproject_cwd = tmp_path / "packages" / "app"
    target = subproject_cwd / "src" / "sample.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n")
    output = json.dumps(
        {
            "findings": [
                {
                    "code": "DUP001",
                    "lines": 12,
                    "context": "function",
                    "scope": "module",
                    "occurrences": 1,
                    "distribution": "same-file",
                    "locations": [{"path": "src/sample.py", "start_line": 1, "end_line": 12}],
                }
            ]
        }
    )
    proc = pyfltr.command.process.CompletedProcessWithTimeoutInfo(args=["arid"], returncode=1, stdout=output)
    mocker.patch("pyfltr.command.process.run_process", return_value=proc)
    config = pyfltr.config.config.create_default_config()
    config.values["arid"] = True
    relative_target = target.relative_to(tmp_path)
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[relative_target],
        cache_store=None,
        cache_run_id=None,
        start_cwd=tmp_path,
        subproject_files={subproject_cwd: [relative_target]},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base, subproject_cwd=subproject_cwd)

    result = pyfltr.command.dispatcher.execute_command("arid", _testconf.make_args(), ctx)

    assert [error.file for error in result.errors] == ["packages/app/src/sample.py"]


def test_execute_command_pytest_config_conflict_emits_warning(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample_test.py"
    target.write_text("def test_sample():\n    assert True\n")
    proc = pyfltr.command.process.CompletedProcessWithTimeoutInfo(
        args=["pytest"],
        returncode=0,
        stdout="""============================= test session starts ==============================
rootdir: /tmp/project
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
""",
    )
    mocker.patch("pyfltr.command.process.run_process", return_value=proc)
    config = pyfltr.config.config.create_default_config()
    config.values["pytest"] = True
    pyfltr.warnings_.clear()
    try:
        result = pyfltr.command.dispatcher.execute_command(
            "pytest",
            _testconf.make_args(),
            _testconf.make_execution_context(config, [target], start_cwd=tmp_path),
        )
        warnings = [warning for warning in pyfltr.warnings_.collected_warnings() if warning["source"] == "config-conflict"]
        assert result.status == "succeeded"
        assert len(warnings) == 1
        assert "pytest.ini" in warnings[0]["message"]
        assert "pyproject.toml" in warnings[0]["message"]
        assert "設定を集約" in warnings[0]["hint"]
    finally:
        pyfltr.warnings_.clear()


def test_pass_filenames_false_omits_targets(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.ts"
    target.write_text("const x = 1;\n")

    proc = subprocess.CompletedProcess(["tsc"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["tsc"] = True
    assert config["tsc-pass-filenames"] is False

    result = pyfltr.command.dispatcher.execute_command(
        "tsc", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(target) not in cmdline
    assert result.status == "succeeded"


def test_pass_filenames_true_includes_targets(mocker, tmp_path: pathlib.Path) -> None:
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
    assert str(target) in cmdline
    assert result.status == "succeeded"


def test_execute_command_cache_hit_skips_subprocess(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "foo.md"
    target.write_text("# title\n")
    cache_root = tmp_path / ".cache"
    store = pyfltr.state.cache.CacheStore(cache_root=cache_root)

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess")

    config = pyfltr.config.config.create_default_config()
    config.values["textlint"] = True
    config.values["textlint-path"] = "/bin/true"

    mock_run.return_value = subprocess.CompletedProcess(["textlint"], returncode=0, stdout="ok")
    result1 = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01ABCDEFGH", start_cwd=tmp_path),
    )
    assert mock_run.call_count == 1
    assert result1.cached is False
    assert result1.cached_from is None

    result2 = pyfltr.command.dispatcher.execute_command(
        "textlint",
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], cache_store=store, cache_run_id="01XYZ", start_cwd=tmp_path),
    )
    assert mock_run.call_count == 1
    assert result2.cached is True
    assert result2.cached_from == "01ABCDEFGH"


def test_execute_command_non_cacheable_skips_cache(mocker, tmp_path: pathlib.Path) -> None:
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
    assert not list(cache_root.rglob("*.json"))


def test_execute_command_only_failed_targets_files_override(mocker, tmp_path: pathlib.Path) -> None:
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
            config, [file_a, file_b], only_failed_targets=pyfltr.command.only_failed.ToolTargets.with_files([file_b])
        ),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_b) in cmdline
    assert str(file_a) not in cmdline
    assert result.target_files == [file_b]


def test_execute_command_only_failed_targets_fallback_uses_all_files(mocker, tmp_path: pathlib.Path) -> None:
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
            config, [file_a], only_failed_targets=pyfltr.command.only_failed.ToolTargets.fallback_default()
        ),
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert str(file_a) in cmdline


def test_execute_command_only_failed_targets_none_uses_default(mocker, tmp_path: pathlib.Path) -> None:
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


def test_execute_command_uv_runner_on_non_python_tool_emits_runner_change_hint(
    tmp_path: pathlib.Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("hello\n")

    config = pyfltr.config.config.create_default_config()
    config.values["typos"] = True
    config.values["typos-runner"] = "uv"

    result = pyfltr.command.dispatcher.execute_command(
        "typos", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )
    assert result.status == "resolution_failed"
    warnings = pyfltr.warnings_.collected_warnings()
    runner_warnings = [w for w in warnings if w["source"] == "tool-resolve" and "Python系" in w["message"]]
    assert len(runner_warnings) == 1
    assert "PYTHON_TOOL_BIN" not in runner_warnings[0]["message"]
    assert "typos-runner" in runner_warnings[0].get("hint", "")
    assert "direct" in runner_warnings[0]["hint"]


def test_execute_command_uv_path_does_not_have_emits_uv_add_hint(
    mocker, tmp_path: pathlib.Path, setup_uv_runner: typing.Callable[..., None]
) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n")

    setup_uv_runner(uv_lock=True, uv_available=True)

    proc = subprocess.CompletedProcess(
        ["uv", "run", "--frozen", "mypy"],
        returncode=2,
        stdout="error: project 'sample' does not have 'mypy' as a dependency\n",
    )
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True

    pyfltr.command.dispatcher.execute_command("mypy", _testconf.make_args(), _testconf.make_execution_context(config, [target]))

    warnings = pyfltr.warnings_.collected_warnings()
    uv_warnings = [w for w in warnings if w["source"] == "tool-resolve" and "uv経路でのツール起動に失敗" in w["message"]]
    assert len(uv_warnings) == 1
    hint = uv_warnings[0].get("hint", "")
    assert "uv add --dev" in hint
    assert "pyfltr[python]" in hint


@dataclasses.dataclass(frozen=True)
class _CapturedCall:
    cwd: pathlib.Path | None
    env: dict[str, str]


@pytest.fixture(name="captured_calls")
def _captured_calls(monkeypatch: pytest.MonkeyPatch) -> list[_CapturedCall]:
    captured: list[_CapturedCall] = []

    def _fake_run(
        commandline: list[str],
        env: dict[str, str],
        on_output: typing.Callable[[str], None] | None = None,
        *,
        cwd: pathlib.Path | None = None,
        **kwargs: object,
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        del on_output, kwargs
        captured.append(_CapturedCall(cwd=cwd, env=dict(env)))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=commandline,
            returncode=0,
            stdout="",
            timeout_exceeded=False,
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)
    return captured


def _captured_cwds(calls: list[_CapturedCall]) -> list[pathlib.Path | None]:
    return [call.cwd for call in calls]


@pytest.mark.parametrize(
    ("command", "suffix"),
    [
        pytest.param("typos", ".txt", id="plain"),
        pytest.param("prettier", ".js", id="prettier"),
    ],
)
def test_single_project_uses_start_cwd(
    command: str,
    suffix: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_calls: list[_CapturedCall],
) -> None:
    work_dir = tmp_path / "project"
    work_dir.mkdir()
    target = pathlib.Path(f"sample{suffix}")
    (work_dir / target).write_text("const value = 1;\n" if suffix == ".js" else "value\n", encoding="utf-8")
    server_cwd = tmp_path / "server"
    server_cwd.mkdir()
    monkeypatch.chdir(server_cwd)

    config = pyfltr.config.config.create_default_config()
    config.values[command] = True
    config.values[f"{command}-path"] = command
    ctx = _testconf.make_execution_context(config, [target], start_cwd=work_dir)

    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    assert result.status == "succeeded"
    assert captured_calls
    assert set(_captured_cwds(captured_calls)) == {work_dir}
    assert pathlib.Path.cwd() == server_cwd


def test_subproject_overrides_start_cwd(
    tmp_path: pathlib.Path,
    captured_calls: list[_CapturedCall],
) -> None:
    start_cwd = tmp_path / "repo"
    subproject_cwd = start_cwd / "package"
    subproject_cwd.mkdir(parents=True)
    target = pathlib.Path("package/sample.txt")
    (start_cwd / target).write_text("value\n", encoding="utf-8")
    config = pyfltr.config.config.create_default_config()
    config.values["typos"] = True
    config.values["typos-path"] = "typos"
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[target],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subproject_files={subproject_cwd: [target]},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base, subproject_cwd=subproject_cwd)

    result = pyfltr.command.dispatcher.execute_command("typos", _testconf.make_args(), ctx)

    assert result.status == "succeeded"
    assert _captured_cwds(captured_calls) == [subproject_cwd]


def _setup_python_tool_runner(monkeypatch: pytest.MonkeyPatch, *, uv_available: bool) -> None:
    monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda *_args: True)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: uv_available)
    if not uv_available:
        monkeypatch.setattr(
            "pyfltr.command.runner.shutil.which",
            lambda name: f"/fake/bin/{name}" if name in pyfltr.command.runner.PYTHON_TOOL_BIN.values() else None,
        )


def _make_python_target(work_dir: pathlib.Path) -> pathlib.Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    target = pathlib.Path("sample.py")
    (work_dir / target).write_text("value = 1\n", encoding="utf-8")
    return target


def test_uv_runner_drops_virtual_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_calls: list[_CapturedCall],
) -> None:
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "other" / ".venv"))
    _setup_python_tool_runner(monkeypatch, uv_available=True)
    work_dir = tmp_path / "project"
    target = _make_python_target(work_dir)

    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True
    ctx = _testconf.make_execution_context(config, [target], start_cwd=work_dir)

    result = pyfltr.command.dispatcher.execute_command("mypy", _testconf.make_args(), ctx)

    assert result.status == "succeeded"
    assert result.effective_runner == "uv"
    assert captured_calls
    assert all("VIRTUAL_ENV" not in call.env for call in captured_calls)


def test_direct_runner_keeps_virtual_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_calls: list[_CapturedCall],
) -> None:
    virtual_env = str(tmp_path / "other" / ".venv")
    monkeypatch.setenv("VIRTUAL_ENV", virtual_env)
    _setup_python_tool_runner(monkeypatch, uv_available=False)
    work_dir = tmp_path / "project"
    target = _make_python_target(work_dir)

    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True
    ctx = _testconf.make_execution_context(config, [target], start_cwd=work_dir)

    result = pyfltr.command.dispatcher.execute_command("mypy", _testconf.make_args(), ctx)

    assert result.status == "succeeded"
    assert result.effective_runner == "direct"
    assert captured_calls
    assert all(call.env["VIRTUAL_ENV"] == virtual_env for call in captured_calls)


def test_subproject_uv_run_uses_subproject_cwd_without_virtual_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_calls: list[_CapturedCall],
) -> None:
    start_cwd = tmp_path / "repo"
    subproject_cwd = start_cwd / "package"
    monkeypatch.setenv("VIRTUAL_ENV", str(start_cwd / ".venv"))
    _setup_python_tool_runner(monkeypatch, uv_available=True)
    subproject_cwd.mkdir(parents=True)
    target = pathlib.Path("package/sample.py")
    (start_cwd / target).write_text("value = 1\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    config.values["mypy"] = True
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[target],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subproject_files={subproject_cwd: [target]},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base, subproject_cwd=subproject_cwd)

    result = pyfltr.command.dispatcher.execute_command("mypy", _testconf.make_args(), ctx)

    assert result.status == "succeeded"
    assert _captured_cwds(captured_calls) == [subproject_cwd]
    assert all("VIRTUAL_ENV" not in call.env for call in captured_calls)


@pytest.mark.parametrize(("enabled", "has_history"), [(False, False), (True, False), (True, True)])
def test_pytest_impact_preserves_explicit_scope(
    tmp_path: pathlib.Path, mocker: typing.Any, *, enabled: bool, has_history: bool
) -> None:
    config = pyfltr.config.config.create_default_config()
    config.values.update({"pytest": True, "pytest-impact-selection": enabled, "respect-gitignore": False})
    requested = pathlib.Path("requested_test.py")
    other = pathlib.Path("other_test.py")
    for target in (requested, other):
        (tmp_path / target).touch()
    probe = mocker.patch(
        "pyfltr.command.pytest_impact.probe",
        return_value=(
            pyfltr.command.pytest_impact.Probe(
                tests=("other_test.py::test_other",), known=("requested_test.py::test_requested",)
            )
            if has_history
            else pyfltr.command.pytest_impact.Probe(reason="履歴欠損")
        ),
    )
    env = {"TESTSCOPE": "existing"}
    mocker.patch("pyfltr.command.env.build_subprocess_env", return_value=env.copy())
    mocker.patch("pyfltr.command.runner.shutil.which", return_value=str(tmp_path / "pytest"))
    process = mocker.patch(
        "pyfltr.command.process.run_process",
        return_value=pyfltr.command.process.CompletedProcessWithTimeoutInfo(args=["pytest"], returncode=0, stdout=""),
    )
    args = pyfltr.run_options.RunOptions(targets=[requested])
    ctx = _testconf.make_execution_context(config, [requested], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("pytest", args, ctx)
    assert not result.failed
    assert probe.call_count == int(enabled)
    assert result.target_files == [requested]
    request = process.call_args.args[0]
    assert request.env["TESTSCOPE"] == ("off" if enabled else "existing")
    assert "--testscope-record" not in request.params.commandline
