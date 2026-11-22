import argparse
import pathlib
import typing

import pytest

import pyfltr.cli.main
import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.subprojects
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.warnings_
from tests import conftest as _testconf

_DEFAULT_PREFIXES: dict[str, list[str]] = {
    "markdownlint": ["pnpx", "markdownlint-cli2"],
    "textlint": ["pnpx", "textlint"],
    "pre-commit": ["pre-commit", "run"],
    "prek": ["prek", "run"],
    "pytest": ["pytest"],
    "vitest": ["pnpx", "vitest"],
    "cargo-test": ["cargo", "test"],
    "dotnet-test": ["dotnet", "test"],
    "gitleaks": ["gitleaks"],
    "semgrep": ["semgrep"],
    "ruff-check": ["ruff", "check"],
}


def _patch_build_commandline(monkeypatch: pytest.MonkeyPatch) -> None:

    def _fake_build_commandline(
        command: str,
        config: pyfltr.config.model.Config,
        *,
        allow_side_effects: bool = False,
        cwd: pathlib.Path | None = None,
        uv_workspace_root: pathlib.Path | None = None,
    ) -> pyfltr.command.runner.ResolvedCommandline:
        del config, allow_side_effects, cwd, uv_workspace_root
        prefix = _DEFAULT_PREFIXES.get(command, [command])
        return pyfltr.command.runner.ResolvedCommandline(
            executable=prefix[0],
            prefix=prefix[1:],
            runner="direct",
            runner_source="default",
            effective_runner="direct",
        )

    def _fake_ensure_mise_available(
        resolved: pyfltr.command.runner.ResolvedCommandline,
        config: pyfltr.config.model.Config,
        *,
        command: str,
        cwd: pathlib.Path | None = None,
    ) -> pyfltr.command.runner.ResolvedCommandline:
        del config, command, cwd
        return resolved

    monkeypatch.setattr(pyfltr.command.runner, "build_commandline", _fake_build_commandline)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_mise_available", _fake_ensure_mise_available)


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    captured: list[list[str]] = []

    def _fake_run_subprocess(
        commandline: list[str],
        env: dict[str, str],
        on_output: typing.Callable[[str], None] | None = None,
        **kwargs: object,
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        del env, on_output, kwargs
        captured.append(list(commandline))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=commandline,
            returncode=0,
            stdout="",
            timeout_exceeded=False,
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run_subprocess)
    return captured


def _enable(config: pyfltr.config.model.Config, commands: typing.Iterable[str]) -> None:
    for c in commands:
        config.values[c] = True


def _make_external(tmp_path: pathlib.Path) -> pathlib.Path:
    parent = tmp_path.parent / f"external-{tmp_path.name}"
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / "plan.md"
    target.write_text("# plan\n", encoding="utf-8")
    return target.resolve()


@pytest.fixture
def _ext_workspace(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    internal = tmp_path / "doc.md"
    internal.write_text("# internal\n", encoding="utf-8")
    external = _make_external(tmp_path)
    return tmp_path, internal, external


def test_is_external_path_relative_is_internal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    relative = pathlib.Path("a_test.py")
    (tmp_path / "a_test.py").write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [relative], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []


def test_is_external_path_absolute_inside_is_internal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    inside = tmp_path / "inside_test.py"
    inside.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [inside.resolve()], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []


def test_is_external_path_absolute_outside_is_external(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-iep"
    ext_dir.mkdir(parents=True, exist_ok=True)
    outside = (ext_dir / "ext_test.py").resolve()
    outside.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [outside], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    # テストで使うパスがWindows区切りを含まないため、実装と独立した`as_posix()`で期待値を生成する。
    assert outside.as_posix() in pyfltr.warnings_.filtered_direct_files(reason="external")
    external_warnings = [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "external-path"]
    assert external_warnings
    assert "--allow-external-paths" in external_warnings[0]["hint"]


def test_user_overrides_config_detects_equal_form(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    config.values["markdownlint-args"] = ["--config=/user/path"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert "--config=/user/path" in result.commandline
    assert str((tmp_path / ".markdownlint.json").resolve()) not in result.commandline


def test_user_overrides_config_detects_in_extend_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    config.values["markdownlint-extend-args"] = ["--config", "/extend/path"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert str((tmp_path / ".markdownlint.json").resolve()) not in result.commandline


def test_user_overrides_config_negative(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / ".markdownlint.json").write_text("{}", encoding="utf-8")
    internal = tmp_path / "doc.md"
    internal.write_text("# doc\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    config.values["markdownlint-args"] = ["--format", "json"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert "--config" in result.commandline
    assert str((tmp_path / ".markdownlint.json").resolve()) in result.commandline


def test_bandit_config_injection_uses_pyproject_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.bandit]\n", encoding="utf-8")
    internal = tmp_path / "code.py"
    internal.write_text("import subprocess\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["bandit"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("bandit", _testconf.make_args(), ctx)

    assert "--configfile" in result.commandline
    assert str((tmp_path / "pyproject.toml").resolve()) in result.commandline


def test_bandit_user_overrides_separate_form(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.bandit]\n", encoding="utf-8")
    internal = tmp_path / "code.py"
    internal.write_text("import subprocess\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["bandit"])
    config.values["bandit-args"] = ["--configfile", "/user/bandit.yaml"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("bandit", _testconf.make_args(), ctx)

    assert result.commandline.count("--configfile") == 1
    assert "/user/bandit.yaml" in result.commandline
    assert str((tmp_path / "pyproject.toml").resolve()) not in result.commandline


def test_bandit_user_overrides_equal_form(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.bandit]\n", encoding="utf-8")
    internal = tmp_path / "code.py"
    internal.write_text("import subprocess\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["bandit"])
    config.values["bandit-args"] = ["--configfile=/user/bandit.yaml"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("bandit", _testconf.make_args(), ctx)

    assert "--configfile=/user/bandit.yaml" in result.commandline
    assert str((tmp_path / "pyproject.toml").resolve()) not in result.commandline


def test_bandit_does_not_inject_dotbandit_ini(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / ".bandit").write_text("[bandit]\n", encoding="utf-8")
    internal = tmp_path / "code.py"
    internal.write_text("import subprocess\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["bandit"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("bandit", _testconf.make_args(), ctx)

    assert "--configfile" not in result.commandline
    assert str((tmp_path / ".bandit").resolve()) not in result.commandline


def test_bandit_inject_candidate_priority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.bandit]\n", encoding="utf-8")
    (tmp_path / ".bandit.yaml").write_text("skips: []\n", encoding="utf-8")
    (tmp_path / ".bandit.toml").write_text("skips = []\n", encoding="utf-8")
    internal = tmp_path / "code.py"
    internal.write_text("import subprocess\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["bandit"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("bandit", _testconf.make_args(), ctx)

    assert str((tmp_path / "pyproject.toml").resolve()) in result.commandline
    assert str((tmp_path / ".bandit.yaml").resolve()) not in result.commandline
    assert str((tmp_path / ".bandit.toml").resolve()) not in result.commandline


@pytest.mark.parametrize(
    "command,config_file_name",
    [
        ("markdownlint", ".markdownlint.json"),
        ("textlint", ".textlintrc.yaml"),
    ],
)
def test_inject_config_arg_when_setting_file_exists(
    monkeypatch: pytest.MonkeyPatch,
    _ext_workspace: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    command: str,
    config_file_name: str,
) -> None:
    start_cwd, internal, _external = _ext_workspace
    config_path = start_cwd / config_file_name
    config_path.write_text("# dummy\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    _patch_build_commandline(monkeypatch)
    captured = _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=start_cwd)
    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    assert captured, "subprocess should have been called"
    commandline = result.commandline
    prefix = _DEFAULT_PREFIXES[command]
    prefix_len = len(prefix)
    assert commandline[prefix_len] == "--config"
    assert commandline[prefix_len + 1] == str(config_path.resolve())


@pytest.mark.parametrize("command", ["markdownlint", "textlint"])
def test_inject_skipped_when_no_setting_file(
    monkeypatch: pytest.MonkeyPatch,
    _ext_workspace: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    command: str,
) -> None:
    start_cwd, internal, _external = _ext_workspace
    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=start_cwd)
    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    assert "--config" not in result.commandline


@pytest.mark.parametrize(
    "command,config_file_name",
    [("markdownlint", ".markdownlint.json"), ("textlint", ".textlintrc.yaml")],
)
def test_inject_skipped_when_user_specifies_config(
    monkeypatch: pytest.MonkeyPatch,
    _ext_workspace: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
    command: str,
    config_file_name: str,
) -> None:
    start_cwd, internal, _external = _ext_workspace
    (start_cwd / config_file_name).write_text("# dummy\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    config.values[f"{command}-args"] = ["--config", "/explicit/path"]
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal], start_cwd=start_cwd)
    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    assert result.commandline.count("--config") == 1
    assert "/explicit/path" in result.commandline
    assert str((start_cwd / config_file_name).resolve()) not in result.commandline


@pytest.mark.parametrize(
    "command,target_pattern",
    [
        ("pre-commit", "doc.md"),
        ("prek", "doc.md"),
        ("pytest", "x_test.py"),
        ("gitleaks", "doc.md"),
        ("vitest", "x.test.ts"),
        ("cargo-test", "lib.rs"),
        ("dotnet-test", "Program.cs"),
        ("semgrep", "code.py"),
        ("uv-audit", "pyproject.toml"),
        ("pnpm-audit", "package.json"),
        ("npm-audit", "package.json"),
        ("yarn-audit", "package.json"),
        ("markdownlint", "doc.md"),
        ("textlint", "doc.md"),
    ],
)
def test_external_path_filtered_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    command: str,
    target_pattern: str,
) -> None:
    internal = tmp_path / target_pattern
    internal.write_text("", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}"
    ext_dir.mkdir(parents=True, exist_ok=True)
    audit_commands = {"uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"}
    external_name = target_pattern if command in audit_commands else f"nested\\{target_pattern}"
    external = (ext_dir / pathlib.Path(external_name)).resolve()
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("", encoding="utf-8")
    normalized_external = pyfltr.paths.normalize_separators(external)

    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal, external], start_cwd=tmp_path)
    pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(), ctx)

    filtered = pyfltr.warnings_.filtered_direct_files(reason="external")
    assert normalized_external in filtered
    warnings = pyfltr.warnings_.collected_warnings()
    assert any(command in w["message"] and normalized_external in w["message"] for w in warnings)


def test_external_only_results_in_zero_targets(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-only"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external = (ext_dir / "a_test.py").resolve()
    external.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [external], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    assert result.files == 0


@pytest.mark.parametrize(
    "command,target_pattern",
    [
        ("markdownlint", "doc.md"),
        ("textlint", "doc.md"),
        ("prek", "doc.md"),
        ("pytest", "x_test.py"),
    ],
)
def test_allow_external_paths_keeps_external(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    command: str,
    target_pattern: str,
) -> None:
    internal = tmp_path / target_pattern
    internal.write_text("", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-allow-{command}"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external = (ext_dir / target_pattern).resolve()
    external.write_text("", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, [command])
    _patch_build_commandline(monkeypatch)
    _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal, external], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command(command, _testconf.make_args(allow_external_paths=True), ctx)

    assert str(external) in result.commandline
    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []
    assert not [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "external-path"]


def test_allow_external_paths_via_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    config_file = tmp_path / ".textlintrc.yaml"
    config_file.write_text("rules: {}\n", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-cli"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external = (ext_dir / "doc.md").resolve()
    external.write_text("# doc\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    _patch_build_commandline(monkeypatch)
    captured = _patch_subprocess(monkeypatch)

    exit_code = pyfltr.cli.main.run(
        [
            "run",
            "--allow-external-paths",
            "--enable=textlint",
            "--commands=textlint",
            "--no-fix",
            "--no-archive",
            "--no-cache",
            str(external),
        ]
    )

    assert exit_code == 0
    assert len(captured) == 1
    commandline = captured[0]
    assert str(external) in commandline
    assert "--config" in commandline
    assert str(config_file.resolve()) in commandline
    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []
    assert not [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "external-path"]


def test_pass_through_command_includes_external(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    internal = tmp_path / "a.py"
    internal.write_text("x = 1\n", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-py"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external = (ext_dir / "ext.py").resolve()
    external.write_text("y = 2\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["ruff-check"])
    _patch_build_commandline(monkeypatch)
    captured = _patch_subprocess(monkeypatch)

    ctx = _testconf.make_execution_context(config, [internal, external], start_cwd=tmp_path)
    result = pyfltr.command.dispatcher.execute_command("ruff-check", _testconf.make_args(), ctx)

    assert captured, "subprocess should have been called"
    assert str(internal) in result.commandline or str(internal.resolve()) in result.commandline
    assert str(external) in result.commandline
    assert "--config" not in result.commandline
    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []


def _make_monorepo(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'root'\n", encoding="utf-8")
    sub_a = tmp_path / "pkg_a"
    sub_a.mkdir()
    (sub_a / "pyproject.toml").write_text("[project]\nname = 'a'\n", encoding="utf-8")
    sub_b = tmp_path / "pkg_b"
    sub_b.mkdir()
    (sub_b / "pyproject.toml").write_text("[project]\nname = 'b'\n", encoding="utf-8")
    return tmp_path, sub_a, sub_b


def test_monorepo_internal_only_no_external_dispatch(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    start_cwd, sub_a, sub_b = _make_monorepo(tmp_path)
    file_a = sub_a / "doc.md"
    file_a.write_text("# a\n", encoding="utf-8")
    file_b = sub_b / "doc.md"
    file_b.write_text("# b\n", encoding="utf-8")

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    _patch_build_commandline(monkeypatch)

    subs = pyfltr.command.subprojects.discover_subprojects(start_cwd, config, git_check_ignore=lambda _s, _c: set())
    subproject_files, external_files = pyfltr.command.subprojects.classify_files_by_subproject(
        [pathlib.Path("pkg_a/doc.md"), pathlib.Path("pkg_b/doc.md")], subs, start_cwd
    )
    assert not external_files
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[pathlib.Path("pkg_a/doc.md"), pathlib.Path("pkg_b/doc.md")],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subprojects=subs,
        subproject_files=subproject_files,
        external_files=external_files,
        subproject_configs={s.cwd: config for s in subs},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base)

    calls: list[pathlib.Path | None] = []

    def _capture(command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext) -> object:
        del command, args
        calls.append(c.subproject_cwd)
        return _testconf.make_succeeded_result(command="markdownlint")

    monkeypatch.setattr(pyfltr.command.dispatcher, "_dispatch_command", _capture)
    pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert None not in calls
    assert len(calls) == 2


def _capture_markdownlint_subprojects(
    monkeypatch: pytest.MonkeyPatch, start_cwd: pathlib.Path, external: pathlib.Path
) -> tuple[pyfltr.command.core_.ExecutionContext, list[pathlib.Path | None], pathlib.Path]:
    config = pyfltr.config.config.create_default_config()
    _enable(config, ["markdownlint"])
    _patch_build_commandline(monkeypatch)

    subs = pyfltr.command.subprojects.discover_subprojects(start_cwd, config, git_check_ignore=lambda _s, _c: set())
    sub_a_cwd = next(s.cwd for s in subs if s.relative == "pkg_a")
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[pathlib.Path("pkg_a/doc.md"), external],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subprojects=subs,
        subproject_files={s.cwd: ([pathlib.Path("pkg_a/doc.md")] if s.cwd == sub_a_cwd else []) for s in subs},
        external_files=[external],
        subproject_configs={s.cwd: config for s in subs},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base)

    calls: list[pathlib.Path | None] = []

    def _capture(command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext) -> object:
        del command, args
        calls.append(c.subproject_cwd)
        return _testconf.make_succeeded_result(command="markdownlint")

    monkeypatch.setattr(pyfltr.command.dispatcher, "_dispatch_command", _capture)
    return ctx, calls, sub_a_cwd


def test_monorepo_mixed_inject_target_warns_only(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    start_cwd, sub_a, _sub_b = _make_monorepo(tmp_path)
    file_a = sub_a / "doc.md"
    file_a.write_text("# a\n", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-markdownlint"
    external = (ext_dir / pathlib.Path(r"nested\doc.md")).resolve()
    external.parent.mkdir(parents=True, exist_ok=True)
    external.write_text("# external\n", encoding="utf-8")
    normalized_external = pyfltr.paths.normalize_separators(external)
    (start_cwd / ".markdownlint.json").write_text("{}", encoding="utf-8")

    ctx, calls, sub_a_cwd = _capture_markdownlint_subprojects(monkeypatch, start_cwd, external)
    pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(), ctx)

    assert calls == [sub_a_cwd]
    filtered = pyfltr.warnings_.filtered_direct_files(reason="external")
    assert normalized_external in filtered
    warnings = pyfltr.warnings_.collected_warnings()
    assert any(normalized_external in warning["message"] for warning in warnings)


def test_monorepo_mixed_excluded_tool_warns_only(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    start_cwd, sub_a, _sub_b = _make_monorepo(tmp_path)
    test_file = sub_a / "x_test.py"
    test_file.write_text("def test(): pass\n", encoding="utf-8")
    ext_dir = tmp_path.parent / f"ext-{tmp_path.name}-pytest"
    ext_dir.mkdir(parents=True, exist_ok=True)
    external_test = (ext_dir / pathlib.Path(r"nested\ext_test.py")).resolve()
    external_test.parent.mkdir(parents=True, exist_ok=True)
    external_test.write_text("def test(): pass\n", encoding="utf-8")
    normalized_external = pyfltr.paths.normalize_separators(external_test)

    config = pyfltr.config.config.create_default_config()
    _enable(config, ["pytest"])
    _patch_build_commandline(monkeypatch)

    subs = pyfltr.command.subprojects.discover_subprojects(start_cwd, config, git_check_ignore=lambda _s, _c: set())
    sub_a_cwd = next(s.cwd for s in subs if s.relative == "pkg_a")
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=[pathlib.Path("pkg_a/x_test.py"), external_test],
        cache_store=None,
        cache_run_id=None,
        start_cwd=start_cwd,
        subprojects=subs,
        subproject_files={s.cwd: ([pathlib.Path("pkg_a/x_test.py")] if s.cwd == sub_a_cwd else []) for s in subs},
        external_files=[external_test],
        subproject_configs={s.cwd: config for s in subs},
    )
    ctx = pyfltr.command.core_.ExecutionContext(base=base)

    calls: list[pathlib.Path | None] = []

    def _capture(command: str, args: argparse.Namespace, c: pyfltr.command.core_.ExecutionContext) -> object:
        del command, args
        calls.append(c.subproject_cwd)
        return _testconf.make_succeeded_result(command="pytest")

    monkeypatch.setattr(pyfltr.command.dispatcher, "_dispatch_command", _capture)
    pyfltr.command.dispatcher.execute_command("pytest", _testconf.make_args(), ctx)

    assert calls == [sub_a_cwd]
    assert normalized_external in pyfltr.warnings_.filtered_direct_files(reason="external")
    warnings = pyfltr.warnings_.collected_warnings()
    assert any(normalized_external in warning["message"] for warning in warnings)


def test_monorepo_allow_external_paths_runs_external(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    start_cwd, sub_a, _sub_b = _make_monorepo(tmp_path)
    file_a = sub_a / "doc.md"
    file_a.write_text("# a\n", encoding="utf-8")
    external = _make_external(tmp_path)
    (start_cwd / ".markdownlint.json").write_text("{}", encoding="utf-8")

    ctx, calls, sub_a_cwd = _capture_markdownlint_subprojects(monkeypatch, start_cwd, external)
    pyfltr.command.dispatcher.execute_command("markdownlint", _testconf.make_args(allow_external_paths=True), ctx)

    assert calls == [sub_a_cwd, None]
    assert pyfltr.warnings_.filtered_direct_files(reason="external") == []
    assert not [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "external-path"]
