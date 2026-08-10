"""外部コマンドへ渡す実効cwdの回帰テスト。"""

import pathlib
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.config.config
from tests import conftest as _testconf


@pytest.fixture(name="captured_cwds")
def _captured_cwds(monkeypatch: pytest.MonkeyPatch) -> list[pathlib.Path | None]:
    """外部プロセスを成功へ固定し、受け取ったcwdを返す。"""
    captured: list[pathlib.Path | None] = []

    def _fake_run(
        commandline: list[str],
        env: dict[str, str],
        on_output: typing.Callable[[str], None] | None = None,
        *,
        cwd: pathlib.Path | None = None,
        **kwargs: object,
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        del env, on_output, kwargs
        captured.append(cwd)
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=commandline,
            returncode=0,
            stdout="",
            timeout_exceeded=False,
        )

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess_with_timeout", _fake_run)
    return captured


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
    captured_cwds: list[pathlib.Path | None],
) -> None:
    """単一プロジェクトのplain・Prettierが起点cwdで実行される。"""
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
    assert captured_cwds
    assert set(captured_cwds) == {work_dir}
    assert pathlib.Path.cwd() == server_cwd


def test_subproject_overrides_start_cwd(
    tmp_path: pathlib.Path,
    captured_cwds: list[pathlib.Path | None],
) -> None:
    """サブプロジェクト実行は当該サブプロジェクトcwdを使う。"""
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
    assert captured_cwds == [subproject_cwd]
