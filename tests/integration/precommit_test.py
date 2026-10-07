import argparse
import pathlib
import textwrap
import typing

import pytest

import pyfltr.command.dispatcher
import pyfltr.command.precommit
import pyfltr.command.precommit_guidance
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import tests.execution_request_helper
from tests import conftest as _testconf


@pytest.mark.parametrize("command", ["pre-commit", "prek"])
def test_pre_commit_integration_persistent_failure_is_rerunnable(
    command: str,
    mocker: typing.Any,
    tmp_path: pathlib.Path,
) -> None:
    """2段階で失敗が残る場合は再実行対象のformatter失敗として返す。"""
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")
    processes = [
        pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=[command],
            returncode=1,
            stdout="hook failed",
        ),
        pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=[command],
            returncode=1,
            stdout="hook still failing",
        ),
    ]
    run_subprocess = mocker.patch(
        "pyfltr.command.process.run_process_loop",
        side_effect=processes,
    )
    mocker.patch("pyfltr.command.precommit_guidance.is_running_under_precommit", return_value=False)
    config = pyfltr.config.config.create_default_config()
    config.values[command] = True

    result = pyfltr.command.dispatcher.execute_command(
        command,
        _testconf.make_args(),
        _testconf.make_execution_context(config, [target], start_cwd=tmp_path),
    )

    assert run_subprocess.call_count == 2
    assert result.formatter_failed is True
    assert result.status == "failed"
    assert result.failed is True
    assert result.needs_rerun is True


@pytest.mark.parametrize(
    ("pre_commit_enabled", "prek_enabled", "expects_conflict_warning"),
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (True, True, True),
    ],
)
def test_pre_commit_prek_conflict_warning(
    tmp_path: pathlib.Path,
    pre_commit_enabled: bool,
    prek_enabled: bool,
    expects_conflict_warning: bool,
) -> None:
    """pre-commitとprekの有効化状態に応じて二重実行警告を発行する。"""
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(f"""\
            [tool.pyfltr]
            pre-commit = {str(pre_commit_enabled).lower()}
            prek = {str(prek_enabled).lower()}
        """),
        encoding="utf-8",
    )
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")

    pyfltr.config.config.load_config(config_dir=tmp_path)

    assert bool(_testconf.count_config_warnings("二重実行")) is expects_conflict_warning


def test_execute_pre_commit_without_config_guides_create_or_disable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.pre-commit-config.yamlが無い場合は、スキップした結果と作成・無効化の操作を示す。"""
    monkeypatch.delenv("PRE_COMMIT", raising=False)
    config = pyfltr.config.config.create_default_config()
    result = pyfltr.command.precommit.execute_pre_commit(
        tests.execution_request_helper.make_request(
            command="pre-commit",
            command_info=config.commands["pre-commit"],
            commandline=["pre-commit"],
            targets=[],
            config=config,
            args=argparse.Namespace(verbose=False),
            env=None,
            on_output=None,
            start_time=0.0,
            cwd=tmp_path,
        )
    )
    assert result.status == "skipped"
    assert "スキップしました" in result.output
    assert "`pre-commit = false`" in result.output
