import os
import pathlib
import subprocess
import time
import typing

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
import tests.execution_request_helper


def _make_glab_ci_lint_args() -> pyfltr.run_options.RunOptions:
    """`execute_glab_ci_lint`で参照される最低限の属性を持つNamespaceを返す。"""
    return pyfltr.run_options.RunOptions.from_values({"verbose": False})


def _make_glab_ci_lint_command_info() -> pyfltr.tools.CommandInfo:
    return pyfltr.tools.BUILTIN_COMMANDS["glab-ci-lint"]


def _execute_glab_ci_lint_case(mocker, tmp_path: pathlib.Path, output: str) -> pyfltr.command.core_.CommandResult:
    """glabの出力だけを変えて同じlint実行経路を検証する。"""
    proc = subprocess.CompletedProcess(args=["glab", "ci", "lint"], returncode=1, stdout=output)
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")
    return pyfltr.command.glab.execute_glab_ci_lint(
        tests.execution_request_helper.make_request(
            command="glab-ci-lint",
            command_info=_make_glab_ci_lint_command_info(),
            commandline=["glab", "ci", "lint"],
            targets=[target],
            config=pyfltr.config.config.create_default_config(),
            env={"PATH": os.environ.get("PATH", "")},
            on_output=None,
            start_time=time.perf_counter(),
            args=_make_glab_ci_lint_args(),
        )
    )


def test_execute_glab_ci_lint_skips_on_host_missing(mocker, tmp_path: pathlib.Path) -> None:
    """ホスト未検出stderrを検出したらreturncode=Noneでスキップ扱いに書き換える。"""
    pyfltr.warnings_.clear()
    result = _execute_glab_ci_lint_case(
        mocker,
        tmp_path,
        "Error: none of the git remotes configured for this repository point to a known GitLab host.\n",
    )

    assert result.returncode is None
    assert result.status == "skipped"
    assert "スキップしました" in result.output
    # スキップの結果だけでなく、ホストを設定する操作と無効化する操作を案内する
    assert "GITLAB_HOST" in result.output
    warnings = pyfltr.warnings_.collected_warnings()
    glab_warnings = [w for w in warnings if w.get("source") == "glab-ci-lint"]
    assert glab_warnings
    assert "glab auth login" in glab_warnings[0]["hint"]
    assert "glab-ci-lint = false" in glab_warnings[0]["hint"]


def test_execute_glab_ci_lint_skips_on_wrapped_host_missing(mocker, tmp_path: pathlib.Path) -> None:
    """端末幅で折り返された文言（Windows runner相当）でもスキップ扱いに書き換える。"""
    pyfltr.warnings_.clear()
    result = _execute_glab_ci_lint_case(
        mocker,
        tmp_path,
        (
            "ERROR  \n"
            "          \n"
            "  You must be in a GitLab project repository for this action: none of"
            " the git remotes configured for this repository  \n"
            "  point to a known GitLab host. Please use `glab auth login` to"
            " authenticate and configure a new host for glab.       \n"
        ),
    )

    assert result.returncode is None
    assert result.status == "skipped"
    assert "スキップしました" in result.output


def test_execute_glab_ci_lint_skips_on_not_authenticated(mocker, tmp_path: pathlib.Path) -> None:
    """大文字の未認証文言 "NOT AUTHENTICATED" も大小文字差を吸収してスキップ扱いに書き換える。"""
    pyfltr.warnings_.clear()
    result = _execute_glab_ci_lint_case(mocker, tmp_path, "you are NOT AUTHENTICATED to glab\n")

    assert result.returncode is None
    assert result.status == "skipped"
    assert "スキップしました" in result.output
    warnings = pyfltr.warnings_.collected_warnings()
    assert any(w.get("source") == "glab-ci-lint" for w in warnings)


def test_execute_glab_ci_lint_keeps_failure_for_real_errors(mocker, tmp_path: pathlib.Path) -> None:
    """ホスト未検出以外の非ゼロ終了はfailedのまま据え置く。"""
    pyfltr.warnings_.clear()
    proc = subprocess.CompletedProcess(
        args=["glab", "ci", "lint"],
        returncode=1,
        stdout="Error: validation failed: jobs:test config key may not be used with `rules`\n",
    )
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")

    result = pyfltr.command.glab.execute_glab_ci_lint(
        tests.execution_request_helper.make_request(
            command="glab-ci-lint",
            command_info=_make_glab_ci_lint_command_info(),
            commandline=["glab", "ci", "lint"],
            targets=[target],
            config=pyfltr.config.config.create_default_config(),
            env={"PATH": os.environ.get("PATH", "")},
            on_output=None,
            start_time=time.perf_counter(),
            args=_make_glab_ci_lint_args(),
        )
    )

    assert result.returncode == 1
    assert result.status == "failed"


def test_execute_glab_ci_lint_passes_through_success(mocker, tmp_path: pathlib.Path) -> None:
    """正常終了はそのままsucceededとして扱い、ロケール強制環境変数を渡す。"""
    pyfltr.warnings_.clear()
    proc = subprocess.CompletedProcess(
        args=["glab", "ci", "lint"],
        returncode=0,
        stdout="OK\n",
    )
    target = tmp_path / ".gitlab-ci.yml"
    target.write_text("stages: [test]\n", encoding="utf-8")

    captured_env: dict[str, str] = {}

    def _capture(*_args: typing.Any, **_kwargs: typing.Any) -> subprocess.CompletedProcess[str]:
        captured_env.update(_args[1])
        return proc

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture)

    result = pyfltr.command.glab.execute_glab_ci_lint(
        tests.execution_request_helper.make_request(
            command="glab-ci-lint",
            command_info=_make_glab_ci_lint_command_info(),
            commandline=["glab", "ci", "lint"],
            targets=[target],
            config=pyfltr.config.config.create_default_config(),
            env={"PATH": os.environ.get("PATH", "")},
            on_output=None,
            start_time=time.perf_counter(),
            args=_make_glab_ci_lint_args(),
        )
    )

    assert result.returncode == 0
    assert result.status == "succeeded"
    # ロケール非依存判定のための環境変数強制を確認する。
    assert captured_env["LC_ALL"] == "C"
    assert captured_env["LANG"] == "C"
