import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_config
import pyfltr.parsing.pytest_nested
import pyfltr.tools
import tests.parsing.conftest


def test_mask_pytest_captured_child_runs_keeps_line_structure() -> None:
    child = tests.parsing.conftest.child_pytest_run(
        file="child/z_test.py", test="test_phantom", line=2, message="assert 1 == 2"
    ).replace("collected 1 item\n", "collected 1 item\nbefore\x0cafter\n")
    output = (
        "================================= FAILURES =================================\n"
        "_______________________________ test_p ________________________________\n"
        "tests/p_test.py:3: in test_p\n"
        "E   assert 1 == 2\n"
        "-------------------------- Captured stdout call ---------------------------\n"
        + child
        + "========================= short test summary info ==========================\n"
        "FAILED tests/p_test.py::test_p - assert 1 == 2\n"
        "================================= 1 failed in 0.30s =================================\n"
    )
    masked = pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs(output)
    before, after = output.split(child, maxsplit=1)
    assert masked == before + "\n" * child.count("\n") + after
