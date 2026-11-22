import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_config
import pyfltr.parsing.pytest_nested
import pyfltr.tools


def test_detect_pytest_config_conflict_single_ignored() -> None:
    output = """============================= test session starts ==============================
rootdir: /tmp/project
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    message = pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output)
    assert message is not None
    assert "pytest.ini" in message
    assert "pyproject.toml" in message


def test_detect_pytest_config_conflict_multiple_ignored() -> None:
    output = """============================= test session starts ==============================
rootdir: /tmp/project
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml, tox.ini, setup.cfg!)
"""
    message = pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output)
    assert message is not None
    assert "pyproject.toml, tox.ini, setup.cfg" in message


def test_detect_pytest_config_conflict_colored_header() -> None:
    output = """\x1b[1m============================= test session starts =============================\x1b[0m
rootdir: /tmp/project
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is not None


@pytest.mark.parametrize(
    "output",
    [
        "configfile: pyproject.toml\n",
        "rootdir: /tmp/x\n",
        "",
    ],
)
def test_detect_pytest_config_conflict_absent(output: str) -> None:
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_ignores_captured_stdout() -> None:
    output = """============================= test session starts ==============================
rootdir: /tmp/project
configfile: pyproject.toml
collected 1 item

sample_test.py configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_absent_in_full_header() -> None:
    output = """============================= test session starts ==============================
platform linux -- Python 3.11.9, pytest-9.1.1, pluggy-1.5.0
rootdir: /tmp/project
configfile: pyproject.toml
collected 1 item
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_ignores_nested_child_run() -> None:
    output = """============================= test session starts ==============================
rootdir: /tmp/project
configfile: pyproject.toml
collected 1 item

sample_test.py F                                                         [100%]

=================================== FAILURES ===================================
________________________________ test_sample ___________________________________
----------------------------- Captured stdout call -----------------------------
============================= test session starts ==============================
rootdir: /tmp/child
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_quiet_parent_with_nested_child() -> None:
    output = """F                                                                        [100%]
=================================== FAILURES ===================================
________________________________ test_sample ___________________________________
----------------------------- Captured stdout call -----------------------------
============================= test session starts ==============================
rootdir: /tmp/child
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_with_stderr_preamble() -> None:
    output = """/path/to/pytest_asyncio/plugin.py:299: PytestDeprecationWarning: The configuration option is unset.
The event loop scope for asynchronous fixtures will default to the "fixture" caching scope.

  warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))
============================= test session starts ==============================
platform linux -- Python 3.11.12, pytest-9.1.1, pluggy-1.6.0
rootdir: /tmp/project
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
collected 1 item
"""
    message = pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output)
    assert message is not None
    assert "pytest.ini" in message
    assert "pyproject.toml" in message


def test_detect_pytest_config_conflict_xdist_ignores_nested_child_run() -> None:
    output = """============================= test session starts ==============================
platform linux -- Python 3.11.12, pytest-9.1.1, pluggy-1.6.0
rootdir: /tmp/project
configfile: pyproject.toml
plugins: xdist-3.8.0
created: 2/2 workers
2 workers [1 item]

F                                                                        [100%]
=================================== FAILURES ===================================
________________________________ test_sample ___________________________________
----------------------------- Captured stdout call -----------------------------
============================= test session starts ==============================
rootdir: /tmp/child
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_xdist_no_header_ignores_nested_child_run() -> None:
    output = """============================= test session starts ==============================
created: 2/2 workers
2 workers [1 item]

F                                                                        [100%]
=================================== FAILURES ===================================
________________________________ test_sample ___________________________________
----------------------------- Captured stdout call -----------------------------
============================= test session starts ==============================
rootdir: /tmp/child
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_xdist_detects_own_conflict() -> None:
    output = """============================= test session starts ==============================
platform linux -- Python 3.11.12, pytest-9.1.1, pluggy-1.6.0
rootdir: /tmp/project
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
plugins: xdist-3.8.0
created: 2/2 workers
2 workers [1 item]
"""
    message = pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output)
    assert message is not None
    assert "pytest.ini" in message


def test_detect_pytest_config_conflict_quiet_and_no_capture_excludes_child_run() -> None:
    output = """============================= test session starts ==============================
rootdir: /tmp/child
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
collected 1 item
child_test.py F
============================= 1 failed in 0.03s ==============================
.
1 passed in 0.10s
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None


def test_detect_pytest_config_conflict_requires_rootdir_line() -> None:
    output = """============================= test session starts ==============================
platform linux -- Python 3.11.9, pytest-9.1.1, pluggy-1.5.0
configfile: pytest.ini (WARNING: ignoring pytest config in pyproject.toml!)
"""
    assert pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output) is None
