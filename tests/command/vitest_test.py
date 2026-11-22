import json
import pathlib
import subprocess

import pyfltr.command.dispatcher
import pyfltr.config.config
from tests import conftest as _testconf


def _capture_outputfile_paths(captured_paths: list[pathlib.Path]):

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        for arg in cmdline:
            if isinstance(arg, str) and arg.startswith("--outputFile.json="):
                captured_paths.append(pathlib.Path(arg.split("=", 1)[1]))
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    return fake_run


def test_vitest_default_args_injects_reporter_and_outputfile(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")
    captured: list[pathlib.Path] = []
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture_outputfile_paths(captured))

    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target], start_cwd=tmp_path)
    )

    assert mock_run.call_count == 1
    cmdline = mock_run.call_args_list[0][0][0]
    assert "--reporter=default" in cmdline
    assert "--reporter=json" in cmdline
    assert any(isinstance(a, str) and a.startswith("--outputFile.json=") for a in cmdline)
    assert len(captured) == 1
    assert not captured[0].exists()


def test_vitest_reporter_user_override_skips_injection(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")
    captured: list[pathlib.Path] = []
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture_outputfile_paths(captured))

    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    config.values["vitest-args"] = ["run", "--passWithNoTests", "--reporter=verbose"]
    pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target], start_cwd=tmp_path)
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert "--reporter=default" not in cmdline
    assert "--reporter=json" not in cmdline
    assert not any(isinstance(a, str) and a.startswith("--outputFile.json=") for a in cmdline)
    assert "--reporter=verbose" in cmdline
    assert not captured


def test_vitest_outputfile_user_override_skips_injection(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")
    captured: list[pathlib.Path] = []
    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_capture_outputfile_paths(captured))

    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    config.values["vitest-args"] = ["run", "--passWithNoTests", "--outputFile=custom-result.json"]
    pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target], start_cwd=tmp_path)
    )

    cmdline = mock_run.call_args_list[0][0][0]
    assert "--reporter=default" not in cmdline
    assert "--reporter=json" not in cmdline
    assert not any(isinstance(a, str) and a.startswith("--outputFile.json=") for a in cmdline)
    assert "--outputFile=custom-result.json" in cmdline
    assert not captured


def test_vitest_sets_slow_tests_from_json(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        output_path = next(
            pathlib.Path(arg.split("=", 1)[1])
            for arg in cmdline
            if isinstance(arg, str) and arg.startswith("--outputFile.json=")
        )
        output_path.write_text(
            json.dumps(
                {
                    "testResults": [
                        {
                            "name": str(target),
                            "assertionResults": [{"fullName": "suite test", "duration": 1500}],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmdline, returncode=0, stdout="")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)
    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target], start_cwd=tmp_path)
    )
    assert [test.to_dict() for test in result.slow_tests] == [
        {"nodeid": "sample.test.ts::suite test", "phase": "test", "seconds": 1.5}
    ]


def test_vitest_slow_tests_empty_on_stdout_fallback(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.test.ts"
    target.write_text("// vitest target\n")
    mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess(["vitest"], returncode=0, stdout="1 passed"),
    )
    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    config.values["vitest-args"] = ["run", "--reporter=verbose"]
    result = pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target], start_cwd=tmp_path)
    )
    assert not result.slow_tests


def test_vitest_suite_load_failure_becomes_file_diagnostic(mocker, tmp_path: pathlib.Path) -> None:
    target = tmp_path / "sample.test.ts"
    target.write_text('throw new Error("sample failure");\n')

    def fake_run(cmdline, env, on_output, **_kwargs):
        del env, on_output
        output_path = next(
            pathlib.Path(arg.split("=", 1)[1])
            for arg in cmdline
            if isinstance(arg, str) and arg.startswith("--outputFile.json=")
        )
        output_path.write_text(
            json.dumps(
                {
                    "testResults": [
                        {
                            "name": str(target),
                            "status": "failed",
                            "message": "sample failure",
                            "assertionResults": [],
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmdline, returncode=1, stdout=" FAIL  sample.test.ts\n")

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)
    config = pyfltr.config.config.create_default_config()
    config.values["vitest"] = True
    result = pyfltr.command.dispatcher.execute_command(
        "vitest", _testconf.make_args(), _testconf.make_execution_context(config, [target], start_cwd=tmp_path)
    )
    assert result.failed
    assert [(error.file, error.line, error.message) for error in result.errors] == [("sample.test.ts", 1, "sample failure")]
