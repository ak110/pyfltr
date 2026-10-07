import os
import pathlib
import subprocess
import time
import typing

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
import tests.execution_request_helper
from tests import conftest as _testconf


def test_expanduser_expands_check_and_write_args(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """`{command}-check-args` / `{command}-write-args` の `~` が two_step経路で展開される。

    `execute_check_write_two_step` 内部のサブプロセス実行を差し替え、組み立てた
    check_commandline / write_commandlineに展開後のパスが含まれることを検証する。
    """
    monkeypatch.setenv("HOME", "/tmp/fake-home")
    monkeypatch.setenv("USERPROFILE", "/tmp/fake-home")

    config = pyfltr.config.config.create_default_config()
    config.values["taplo-check-args"] = ["fmt", "--check", "--config=~/taplo.toml"]
    config.values["taplo-write-args"] = ["fmt", "--config=~/taplo.toml"]

    captured: list[list[str]] = []

    # `run_process_loop` を差し替えるテストでは戻り値に
    # `pyfltr.command.process.CompletedProcessWithTimeoutInfo` を直接構築する。
    # テスト用ダミークラス（例: `class _FakeProc:`）を関数内で定義すると、pylintの
    # `too-few-public-methods` と `redefined-outer-name` を併発するため避ける。
    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured.append(list(commandline))
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=0, stdout="", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)

    target = tmp_path / "x.toml"
    target.write_text("")
    command_info = config.commands["taplo"]
    args_ns = pyfltr.run_options.RunOptions.from_values({"verbose": False})
    pyfltr.command.two_step.base.execute_check_write_two_step(
        tests.execution_request_helper.make_request(
            command="taplo",
            command_info=command_info,
            commandline_prefix=["taplo"],
            config=config,
            targets=[target],
            additional_args=[],
            fix_mode=False,
            env={},
            on_output=None,
            start_time=time.perf_counter(),
            args=args_ns,
        )
    )

    assert captured, "expected at least the check step to run"
    assert "--config=/tmp/fake-home/taplo.toml" in captured[0]


@pytest.mark.parametrize("command", ["shfmt", "taplo"])
def test_two_step_base_includes_extend_args(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, command: str) -> None:
    """`shfmt`/`taplo`の2段階実行で`common_args`に`{command}-extend-args`が末尾結合される。"""
    config = pyfltr.config.config.create_default_config()
    config.values[f"{command}-args"] = ["--base"]
    config.values[f"{command}-extend-args"] = ["--extra"]

    captured: list[list[str]] = []

    def _fake_run(
        commandline: list[str], *_args: typing.Any, **_kwargs: typing.Any
    ) -> pyfltr.command.process.CompletedProcessWithTimeoutInfo:
        captured.append(list(commandline))
        # rc != 0 でStep2（write）も実行する経路に入る
        return pyfltr.command.process.CompletedProcessWithTimeoutInfo(
            args=list(commandline), returncode=1, stdout="", timeout_exceeded=False
        )

    monkeypatch.setattr(pyfltr.command.process, "run_process_loop", _fake_run)

    target = tmp_path / ("x.toml" if command == "taplo" else "x.sh")
    target.write_text("")
    command_info = config.commands[command]
    args_ns = pyfltr.run_options.RunOptions.from_values({"verbose": False})
    pyfltr.command.two_step.base.execute_check_write_two_step(
        tests.execution_request_helper.make_request(
            command=command,
            command_info=command_info,
            commandline_prefix=[command],
            config=config,
            targets=[target],
            additional_args=[],
            fix_mode=False,
            env={},
            on_output=None,
            start_time=time.perf_counter(),
            args=args_ns,
        )
    )

    # Step1（check）とStep2（write）の両方で--extraが--baseより後に結合される。
    # 直後位置検査だと既定check-args/write-argsの並びに依存して脆いため、
    # 出現順だけを確認する。
    assert len(captured) == 2, f"expected both check and write steps, got {captured}"
    for step in captured:
        assert "--extra" in step
        assert step.index("--extra") > step.index("--base")


def test_taplo_two_step_check_clean(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 (taplo check) rc=0 → succeeded。Step2 (format) は実行されない。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname = "foo"\n')

    proc = subprocess.CompletedProcess(["taplo"], returncode=0, stdout="")
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "check" in cmdline
    assert "format" not in cmdline
    assert result.status == "succeeded"
    assert result.formatter_failed is False


def test_taplo_two_step_check_needs_format(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 → Step2 (format) を実行。rc=0 かつ内容変化ありなら formatted。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname="foo"\n')

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="Cargo.toml is not formatted")
        # format ステップ: ファイルを書き換えてシミュレート
        target.write_text('[package]\nname = "foo"\n')
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    # スナップショット取得後に内容が変化するよう mtime を事前に固定する
    os.utime(target, (1000000000, 1000000000))

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "formatted"
    assert result.formatter_failed is False
    assert result.fixed_files is not None and target.as_posix() in result.fixed_files


def test_taplo_two_step_step2_failure_marks_failed(mocker, tmp_path: pathlib.Path) -> None:
    """Step1 rc=1 でも Step2 の rc!=0 なら failed。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname="foo"\n')

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        if "check" in cmdline:
            return subprocess.CompletedProcess(cmdline, returncode=1, stdout="")
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout="format error")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert result.status == "failed"
    assert result.formatter_failed is True


def test_taplo_fix_mode_skips_check_step(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードでは Step1 (check) をスキップし直接 format を実行する。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname="foo"\n')
    os.utime(target, (1000000000, 1000000000))

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output  # 引数シグネチャ揃えのため受け取るのみ
        # format 実行時にファイルを書き換えてシミュレート
        target.write_text('[package]\nname = "foo"\n')
        os.utime(target, (2000000000, 2000000000))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    # 1 回だけ呼ばれる（Step1 スキップ）
    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "format" in cmdline
    assert "check" not in cmdline
    # ハッシュ変化ありなので formatted
    assert result.status == "formatted"
    assert result.fixed_files is not None and target.as_posix() in result.fixed_files


def test_taplo_fix_mode_no_change_succeeds(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードで format が実行されてもハッシュ変化が無ければ succeeded。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname = "foo"\n')

    proc = subprocess.CompletedProcess(["taplo"], returncode=0, stdout="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "succeeded"
    assert result.fixed_files == []


def test_taplo_fix_mode_process_error_fails(mocker, tmp_path: pathlib.Path) -> None:
    """`--fix` モードで format がエラー終了した場合は failed。"""
    target = tmp_path / "Cargo.toml"
    target.write_text('[package]\nname = "foo"\n')

    proc = subprocess.CompletedProcess(["taplo"], returncode=1, stdout="syntax error")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    config = pyfltr.config.config.create_default_config()
    config.values["taplo"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "taplo", _testconf.make_args(), _testconf.make_execution_context(config, [target], fix_stage=True)
    )

    assert result.status == "failed"
    assert result.formatter_failed is True
