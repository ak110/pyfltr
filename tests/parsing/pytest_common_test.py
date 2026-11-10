import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_config
import pyfltr.parsing.pytest_nested
import pyfltr.tools


def test_pytest_space_doctest_nodeid_keeps_existing_summary_gap() -> None:
    """空白付きdoctest関数の一覧の欠落は基準版と共通の対象外の制約として保持する。"""
    assert not pyfltr.parsing.pytest_common.parse_pytest_summary("FAILED my doc_test.py::my doc_test.f\n")
