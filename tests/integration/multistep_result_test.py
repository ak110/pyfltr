"""dispatcherから各段の異常終了を最終statusまで伝える回帰試験。"""

import pathlib
import typing

import pytest

import pyfltr.command.dispatcher
import pyfltr.command.process
import pyfltr.config.config
from tests import conftest as testconf


@pytest.mark.parametrize(
    ("command", "suffix", "fix_stage", "first_code"),
    [
        ("ruff-format", ".py", False, 0),
        ("taplo", ".toml", False, 1),
        ("shfmt", ".sh", False, 1),
        ("prettier", ".json", False, 1),
        ("prettier", ".json", True, 0),
        ("textlint", ".md", True, 0),
        ("ruff-check", ".py", True, 0),
        ("prek", ".py", False, 1),
    ],
)
@pytest.mark.parametrize("stage", [0, 1])
@pytest.mark.parametrize("code,timeout", [(-9, False), (0, True)])
@pytest.mark.parametrize("changed", [False, True])
def test_stage_failure_reaches_final_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
    command: str,
    suffix: str,
    fix_stage: bool,
    first_code: int,
    stage: int,
    code: int,
    timeout: bool,
    changed: bool,
) -> None:
    """両段のシグナル・timeoutはファイル変更や後段の成功で隠れない。"""
    target = tmp_path / f"sample{suffix}"
    target.write_text("", encoding="utf-8")
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    calls: list[int] = []
    single = fix_stage and command in ("ruff-check", "prettier")
    failing_stage = 0 if single else stage

    def run_process(*_args: typing.Any, **_kwargs: typing.Any) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        index = len(calls)
        calls.append(index)
        if changed:
            target.write_text("changed\n", encoding="utf-8")
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=[],
            returncode=code if index == failing_stage else (first_code if index == 0 else 0),
            stdout=f"stage {index}\n",
            timeout_exceeded=timeout and index == failing_stage,
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process", run_process)
    config = pyfltr.config.config.create_default_config()
    config.values[command] = True
    result = pyfltr.command.dispatcher.execute_command(
        command,
        testconf.make_args(),
        testconf.make_execution_context(config, [target], fix_stage=fix_stage, start_cwd=tmp_path),
    )
    assert result.status == "failed", result
    assert result.returncode != 0
    assert result.timeout_exceeded is timeout
