"""llm_outputのテストコード。"""
# pylint: disable=protected-access

import json

import pyfltr.error_parser
import pyfltr.llm_output


def test_build_diag_record_with_rule_severity_fix() -> None:
    """rule・severity・fixフィールドがdiagレコードに含まれることのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="`os` imported but unused",
        rule="F401",
        severity="error",
        fix="safe",
    )
    record = pyfltr.llm_output._build_diag_record(error)
    assert record["kind"] == "diag"
    assert record["tool"] == "ruff-check"
    assert record["file"] == "src/foo.py"
    assert record["line"] == 10
    assert record["col"] == 5
    assert record["rule"] == "F401"
    assert record["severity"] == "error"
    assert record["fix"] == "safe"
    assert record["msg"] == "`os` imported but unused"

    # msgは最後のキーであることを確認（フィールド順序）
    keys = list(record.keys())
    assert keys[-1] == "msg"


def test_build_diag_record_none_fields_omitted() -> None:
    """rule・severity・fixがNoneのときフィールドが省略されることのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=None,
        command="mypy",
        message="Name 'x' is not defined",
    )
    record = pyfltr.llm_output._build_diag_record(error)
    assert "col" not in record
    assert "rule" not in record
    assert "severity" not in record
    assert "fix" not in record
    assert record["msg"] == "Name 'x' is not defined"


def test_build_diag_record_partial_fields() -> None:
    """一部のフィールドのみ設定されている場合のテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="pylint",
        message="Missing docstring",
        rule="C0114",
        severity="warning",
    )
    record = pyfltr.llm_output._build_diag_record(error)
    assert record["rule"] == "C0114"
    assert record["severity"] == "warning"
    assert "fix" not in record


def test_dump_roundtrip() -> None:
    """_dump()のJSON出力がパース可能であることのテスト。"""
    error = pyfltr.error_parser.ErrorLocation(
        file="src/foo.py",
        line=10,
        col=5,
        command="ruff-check",
        message="`os` imported but unused",
        rule="F401",
        severity="error",
        fix="safe",
    )
    record = pyfltr.llm_output._build_diag_record(error)
    line = pyfltr.llm_output._dump(record)
    parsed = json.loads(line)
    assert parsed["rule"] == "F401"
    assert parsed["severity"] == "error"
    assert parsed["fix"] == "safe"
