"""code_quality のテストコード。"""

import hashlib

import pyfltr.code_quality
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


def test_build_payload_basic() -> None:
    """Code Quality の必須フィールドが揃った JSON 配列が返る。"""
    errors = [_make_error("ruff-check", "src/foo.py", 10, "unused import", col=3)]
    errors[0].rule = "F401"
    errors[0].severity = "error"
    result = _make_result("ruff-check", returncode=1, errors=errors)

    payload = pyfltr.code_quality.build_code_quality_payload([result])

    assert isinstance(payload, list)
    assert len(payload) == 1
    issue = payload[0]
    assert issue["description"] == "unused import"
    assert issue["check_name"] == "ruff-check:F401"
    assert issue["severity"] == "major"
    assert issue["location"] == {"path": "src/foo.py", "lines": {"begin": 10}}
    assert isinstance(issue["fingerprint"], str)
    assert len(issue["fingerprint"]) == 64  # SHA-256 hex 全桁


def test_build_payload_severity_mapping() -> None:
    """severity 3値+未設定がCode Qualityのseverityへマップされる。"""
    errors = [
        _make_error("tool", "a.py", 1, "e"),
        _make_error("tool", "a.py", 2, "w"),
        _make_error("tool", "a.py", 3, "i"),
        _make_error("tool", "a.py", 4, "u"),
    ]
    errors[0].severity = "error"
    errors[1].severity = "warning"
    errors[2].severity = "info"
    # 4件目はseverity未設定のまま
    result = _make_result("tool", returncode=1, errors=errors)

    payload = pyfltr.code_quality.build_code_quality_payload([result])
    severities = [issue["severity"] for issue in payload]
    assert severities == ["major", "minor", "info", "minor"]


def test_build_payload_check_name_without_rule() -> None:
    """ruleが無い場合、check_nameはツール名のみ。"""
    errors = [_make_error("mypy", "a.py", 1, "bad")]
    result = _make_result("mypy", returncode=1, errors=errors)

    payload = pyfltr.code_quality.build_code_quality_payload([result])
    assert payload[0]["check_name"] == "mypy"


def test_build_payload_fingerprint_deterministic() -> None:
    """同じ入力から同じfingerprintが生成され、SHA-256連結仕様と一致する。"""
    err1 = _make_error("ruff-check", "src/foo.py", 10, "unused import", col=3)
    err1.rule = "F401"
    err2 = _make_error("ruff-check", "src/foo.py", 10, "unused import", col=3)
    err2.rule = "F401"

    r1 = _make_result("ruff-check", returncode=1, errors=[err1])
    r2 = _make_result("ruff-check", returncode=1, errors=[err2])
    p1 = pyfltr.code_quality.build_code_quality_payload([r1])
    p2 = pyfltr.code_quality.build_code_quality_payload([r2])
    assert p1[0]["fingerprint"] == p2[0]["fingerprint"]

    expected = hashlib.sha256(b"ruff-check\tsrc/foo.py\t10\t3\tF401\tunused import").hexdigest()
    assert p1[0]["fingerprint"] == expected


def test_build_payload_fingerprint_differs_by_rule() -> None:
    """ruleが異なればfingerprintも異なる。"""
    err1 = _make_error("ruff-check", "a.py", 1, "msg")
    err1.rule = "F401"
    err2 = _make_error("ruff-check", "a.py", 1, "msg")
    err2.rule = "E501"
    r = _make_result("ruff-check", returncode=1, errors=[err1, err2])

    payload = pyfltr.code_quality.build_code_quality_payload([r])
    assert payload[0]["fingerprint"] != payload[1]["fingerprint"]


def test_build_payload_begin_defaults_to_one() -> None:
    """lineが0の場合、location.lines.beginは1に補正される。"""
    errors = [_make_error("pytest", "tests/a.py", 0, "FAIL")]
    result = _make_result("pytest", returncode=1, errors=errors)

    payload = pyfltr.code_quality.build_code_quality_payload([result])
    assert payload[0]["location"]["lines"]["begin"] == 1


def test_build_payload_empty() -> None:
    """エラー無しなら空配列。"""
    result = _make_result("mypy", returncode=0)
    payload = pyfltr.code_quality.build_code_quality_payload([result])
    assert not payload
    assert isinstance(payload, list)
