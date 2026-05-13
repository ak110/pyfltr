"""vitest専用経路の引数注入動作テスト。

`execute_vitest`が`--reporter=default --reporter=json --outputFile.json=<tmpfile>`を
注入する条件と、利用者指定がある場合の注入スキップ、tmpfile後始末を検証する。
"""

import pathlib
import subprocess

import pyfltr.command.dispatcher
import pyfltr.config.config
from tests import conftest as _testconf


def _capture_outputfile_paths(captured_paths: list[pathlib.Path]):
    """`run_subprocess`をパッチして`--outputFile.json=<path>`の値を収集するfakeを返す。

    JSON出力ファイルへの書き込みは行わない（後段でtmpfileが`finally`削除されるか確認するため、
    存在状態を素直に保つ）。
    """

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        for arg in cmdline:
            if isinstance(arg, str) and arg.startswith("--outputFile.json="):
                captured_paths.append(pathlib.Path(arg.split("=", 1)[1]))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    return fake_run


def test_vitest_default_args_injects_reporter_and_outputfile(mocker, tmp_path: pathlib.Path) -> None:
    """既定`vitest-args`では`--reporter=default --reporter=json --outputFile.json=<tmpfile>`が注入される。"""
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")
    captured: list[pathlib.Path] = []
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture_outputfile_paths(captured))

    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--reporter=default" in cmdline
    assert "--reporter=json" in cmdline
    assert any(isinstance(a, str) and a.startswith("--outputFile.json=") for a in cmdline)
    # finally節でtmpfileが削除されていることを確認する。
    assert len(captured) == 1
    assert not captured[0].exists()


def test_vitest_reporter_user_override_skips_injection(mocker, tmp_path: pathlib.Path) -> None:
    """利用者`vitest-args`に`--reporter`がある場合は注入をスキップする。"""
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")
    captured: list[pathlib.Path] = []
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture_outputfile_paths(captured))

    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    config.values["vitest-args"] = ["run", "--passWithNoTests", "--reporter=verbose"]
    pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    cmdline = mock_run.call_args_list[0][0][0]
    # 注入されたフラグは含まれない（利用者の`--reporter=verbose`は維持される）。
    assert "--reporter=default" not in cmdline
    assert "--reporter=json" not in cmdline
    assert not any(isinstance(a, str) and a.startswith("--outputFile.json=") for a in cmdline)
    assert "--reporter=verbose" in cmdline
    assert not captured


def test_vitest_outputfile_user_override_skips_injection(mocker, tmp_path: pathlib.Path) -> None:
    """利用者`vitest-args`に`--outputFile`がある場合は注入をスキップする。"""
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")
    captured: list[pathlib.Path] = []
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture_outputfile_paths(captured))

    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    config.values["vitest-args"] = ["run", "--passWithNoTests", "--outputFile=custom-result.json"]
    pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target])
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert "--reporter=default" not in cmdline
    assert "--reporter=json" not in cmdline
    # 利用者指定`--outputFile=...`は維持され、pyfltr由来の`--outputFile.json=...`は注入されない。
    assert not any(isinstance(a, str) and a.startswith("--outputFile.json=") for a in cmdline)
    assert "--outputFile=custom-result.json" in cmdline
    assert not captured
