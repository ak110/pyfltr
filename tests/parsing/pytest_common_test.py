import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_config
import pyfltr.parsing.pytest_nested
import pyfltr.tools


def test_pytest_space_doctest_nodeid_keeps_existing_summary_gap() -> None:
    assert not pyfltr.parsing.pytest_common.parse_pytest_summary("FAILED my doc_test.py::my doc_test.f\n")
