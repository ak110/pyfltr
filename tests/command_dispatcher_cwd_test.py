"""外部コマンドへ渡す実効cwdと環境の回帰テスト。

実効cwd・実効runner・環境は別々のモジュールが確定するため、1つの検体で同時に観測する。
層ごとに分けた検体では、cwdを切り替えた実行へ実行コンテキスト外の環境が残る不整合を検出できない。
"""

import dataclasses
import pathlib
import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.config
from tests import conftest as _testconf


@dataclasses.dataclass(frozen=True)
class _CapturedCall:
    """擬似実行が受け取ったcwdと環境。"""

    cwd: pathlib.Path | None
    env: dict[str, str]


@pytest.fixture(name="captured_calls")
def _captured_calls(monkeypatch: pytest.MonkeyPatch) -> list[_CapturedCall]:
    """外部プロセスを成功へ固定し、受け取ったcwdと環境を返す。"""
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

    monkeypatch.setattr(pyfltr.command.process, "run_subprocess_with_timeout", _fake_run)
    return captured


def _captured_cwds(calls: list[_CapturedCall]) -> list[pathlib.Path | None]:
    """受け取ったcwdだけを取り出す。"""
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
    assert captured_calls
    assert set(_captured_cwds(captured_calls)) == {work_dir}
    assert pathlib.Path.cwd() == server_cwd


def test_subproject_overrides_start_cwd(
    tmp_path: pathlib.Path,
    captured_calls: list[_CapturedCall],
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
    assert _captured_cwds(captured_calls) == [subproject_cwd]


def _setup_python_tool_runner(monkeypatch: pytest.MonkeyPatch, *, uv_available: bool) -> None:
    """Pythonツールの解決結果をuv経路またはdirect経路へ固定する。"""
    monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda *_args: True)
    monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: uv_available)
    if not uv_available:
        monkeypatch.setattr(
            "pyfltr.command.runner.shutil.which",
            lambda name: f"/fake/bin/{name}" if name in pyfltr.command.runner.PYTHON_TOOL_BIN.values() else None,
        )


def _make_python_target(work_dir: pathlib.Path) -> pathlib.Path:
    """Pythonツールの検査対象を1件生成し、その相対パスを返す。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    target = pathlib.Path("sample.py")
    (work_dir / target).write_text("value = 1\n", encoding="utf-8")
    return target


def test_uv_runner_drops_virtual_env(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    captured_calls: list[_CapturedCall],
) -> None:
    """uv経路の実ツール起動は実行コンテキスト外の`VIRTUAL_ENV`を渡さない。"""
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
    """direct経路では`VIRTUAL_ENV`の継承を維持する。"""
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
    """サブプロジェクトのuv実行は、切替後のcwdと親の`VIRTUAL_ENV`不在を同時に満たす。"""
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
