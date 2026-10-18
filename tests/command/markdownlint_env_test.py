import json
import pathlib
import types
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_server
import pyfltr.command.markdownlint_env
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config


@pytest.fixture
def _environment(tmp_path: pathlib.Path) -> pyfltr.command.markdownlint_env.MarkdownlintEnvironment:
    return pyfltr.command.markdownlint_env.MarkdownlintEnvironment(
        tmp_path / "cache" / "patched",
        {"minimum-release-age": 1440},
        tmp_path,
    )


@pytest.mark.parametrize(("os_name", "suffix"), [("nt", ".cmd"), ("posix", "")])
def test_executable_uses_platform_shim(
    _environment: pyfltr.command.markdownlint_env.MarkdownlintEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    os_name: str,
    suffix: str,
) -> None:
    monkeypatch.setattr(pyfltr.command.markdownlint_env, "os", types.SimpleNamespace(name=os_name))
    assert _environment.executable == str(_environment.root / "node_modules" / ".bin" / f"markdownlint-cli2{suffix}")


@pytest.mark.parametrize("failure", ["nonzero", "unapplied"])
def test_failed_install_is_retried_and_completed_install_is_reused(
    _environment: pyfltr.command.markdownlint_env.MarkdownlintEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    attempts: list[list[str]] = []

    def install(
        argv: list[str], env: dict[str, str], **kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        del env
        attempts.append(argv)
        stage = pathlib.Path(argv[argv.index("--dir") + 1])
        failed_attempt = len(attempts) == 1
        assert kwargs["cwd"] == _environment.cwd
        assert (stage / "braces-3.0.3.patch").read_bytes() == pyfltr.command.markdownlint_env.PATCH_PATH.read_bytes()
        assert json.loads((stage / "package.json").read_text())["pnpm"]["patchedDependencies"] == {
            "braces@3.0.3": "braces-3.0.3.patch"
        }
        if not failed_attempt:
            (stage / "pnpm-lock.yaml").write_text(
                "patchedDependencies:\n  braces@3.0.3:\n    hash: applied\n", encoding="utf-8"
            )
        if not failed_attempt or failure == "unapplied":
            executable = stage / pathlib.Path(_environment.executable).relative_to(_environment.root)
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text("completed", encoding="utf-8")
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=argv,
            returncode=1 if failed_attempt and failure == "nonzero" else 0,
            stdout="install",
            timeout_exceeded=False,
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", install)
    with pytest.raises(ValueError, match="pnpm"):
        pyfltr.command.markdownlint_env.ensure_environment(_environment, timeout=30)
    assert not _environment.root.exists()
    pyfltr.command.markdownlint_env.ensure_environment(_environment, timeout=30)
    pyfltr.command.markdownlint_env.ensure_environment(_environment, timeout=30)
    assert pathlib.Path(_environment.executable).read_text() == "completed"
    assert len(attempts) == 2
    assert "--config.minimum-release-age=1440" in attempts[-1]
    assert attempts[-1][-1] == "markdownlint-cli2"


def test_resource_and_settings_select_separate_environments(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = pyfltr.command.markdownlint_env
    patch = tmp_path / "resource.patch"
    patch.write_text("patch-a", encoding="utf-8")
    monkeypatch.setattr(module, "PATCH_PATH", patch)
    settings = {"minimum-release-age": 1440}
    monkeypatch.setattr(module, "_read_settings", lambda *_args, **_kwargs: settings)
    module.resolve_environment.cache_clear()
    first = module.resolve_environment(tmp_path)
    patch.write_text("patch-b", encoding="utf-8")
    module.resolve_environment.cache_clear()
    second = module.resolve_environment(tmp_path)
    settings["minimum-release-age"] = 2880
    module.resolve_environment.cache_clear()
    third = module.resolve_environment(tmp_path)
    other = module.resolve_environment(tmp_path / "other")
    assert len({first.root, second.root, third.root, other.root}) == 4
    assert "dlx" not in first.root.parts
    module.resolve_environment.cache_clear()


@pytest.mark.parametrize("entry", ["cli", "mcp"])
@pytest.mark.asyncio
async def test_acquisition_failure_reaches_cli_and_mcp_and_retry_succeeds(
    entry: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "sample.md"
    target.write_text("# Sample\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pyfltr]\nmarkdownlint = true\n", encoding="utf-8")
    environment = pyfltr.command.markdownlint_env.MarkdownlintEnvironment(tmp_path / "env", {}, tmp_path)
    monkeypatch.setattr(pyfltr.command.markdownlint_env, "resolve_environment", lambda *_args, **_kwargs: environment)
    acquired = False
    calls: list[list[str]] = []

    def prepare(*_args: typing.Any, **_kwargs: typing.Any) -> None:
        if not acquired:
            raise ValueError("導入失敗。pnpmの設定を確認して再実行してください。")

    def run(
        argv: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        calls.append(argv)
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=argv, returncode=0, stdout="", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.markdownlint_env, "ensure_environment", prepare)
    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", run)
    for expected in ["resolution_failed", "succeeded"]:
        if entry == "cli":
            rc = pyfltr.cli.main.run(
                [
                    "run",
                    str(target),
                    "--work-dir",
                    str(tmp_path),
                    "--commands=markdownlint",
                    "--no-fix",
                    "--no-cache",
                    "--output-format=jsonl",
                    "--no-quiet",
                ]
            )
            records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
            command = next(record for record in reversed(records) if record.get("kind") == "command")
            assert command["status"] == expected
            assert rc == (1 if not acquired else 0)
        else:
            result = await pyfltr.cli.mcp_server.tool_run(
                paths=["sample.md"], commands=["markdownlint"], work_dir=str(tmp_path), no_fix=True, no_cache=True
            )
            assert result.commands[0].status == expected
        if not acquired:
            assert not calls
        acquired = True
    assert calls and pathlib.Path(calls[-1][0]) == pathlib.Path(environment.executable)
