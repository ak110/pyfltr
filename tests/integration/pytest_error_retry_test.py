import json
import pathlib
import shlex
import sys

import pytest

import pyfltr.cli.main

_CONFTEST = """import pytest


@pytest.fixture
def broken_setup():
    raise RuntimeError("setup boom")


@pytest.fixture
def broken_teardown():
    yield 1
    raise RuntimeError("teardown boom")
"""


def _write_project(root: pathlib.Path, files: dict[str, str]) -> None:
    (root / "pyproject.toml").write_text(
        "[tool.pyfltr]\n"
        "pytest = true\n"
        'pytest-runner = "direct"\n'
        f"pytest-path = {json.dumps(sys.executable)}\n"
        'pytest-args = ["-m", "pytest", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:asyncio"]\n'
        "respect-gitignore = false\n",
        encoding="utf-8",
    )
    (root / "conftest.py").write_text(_CONFTEST, encoding="utf-8")
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")


def _run(root: pathlib.Path, capsys: pytest.CaptureFixture[str], targets: list[str]) -> tuple[int, list[dict]]:
    returncode = pyfltr.cli.main.run(
        ["run", "--output-format=jsonl", "--commands=pytest", "--no-cache", "--work-dir", str(root), *targets]
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    return returncode, records


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"setup_test.py": "def test_a(broken_setup):\n    pass\n"}, {"setup_test.py"}),
        (
            {
                "setup_test.py": "def test_a(broken_setup):\n    pass\n",
                "teardown_test.py": "def test_b(broken_teardown):\n    assert 1 == 2\n",
                "ok_test.py": "def test_c():\n    pass\n",
            },
            {"setup_test.py", "teardown_test.py"},
        ),
    ],
)
def test_pytest_errors_reach_diagnostics_and_retry_command(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str], files: dict[str, str], expected: set[str]
) -> None:
    _write_project(tmp_path, files)
    returncode, records = _run(tmp_path, capsys, [str(tmp_path / name) for name in sorted(files)])

    assert returncode == 1
    diagnostics = [record for record in records if record["kind"] == "diagnostic"]
    assert {pathlib.PurePath(record["file"]).name for record in diagnostics} == expected
    messages = [message["msg"] for record in diagnostics for message in record["messages"]]
    assert any("setup error: RuntimeError: setup boom" in message for message in messages)
    command = next(record for record in records if record["kind"] == "command" and record["command"] == "pytest")
    retried = {pathlib.PurePath(arg).name for arg in shlex.split(command["retry_command"]) if arg.endswith("_test.py")}
    assert retried == expected
    if "teardown_test.py" in expected:
        assert any(message.startswith("test_b: assert") for message in messages)
        assert any("test_b: teardown error: RuntimeError: teardown boom" in message for message in messages)
