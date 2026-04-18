"""sarif_outputのテストコード。"""

import pyfltr.config
import pyfltr.sarif_output
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


def test_build_sarif_basic() -> None:
    """SARIF 2.1.0 の基本構造が生成される。"""
    errors = [
        _make_error("ruff-check", "src/foo.py", 10, "unused import"),
    ]
    errors[0].rule = "F401"
    errors[0].severity = "error"
    errors[0].rule_url = "https://docs.astral.sh/ruff/rules/F401/"
    result = _make_result(
        "ruff-check", returncode=1, errors=errors, retry_command="pyfltr run --commands ruff-check -- src/foo.py"
    )

    config = pyfltr.config.create_default_config()
    sarif = pyfltr.sarif_output.build_sarif([result], config, exit_code=1, commands=["ruff-check"], files=1, run_id="01ABC")

    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"].endswith(".json")
    assert len(sarif["runs"]) == 1
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "ruff-check"
    # rules に重複排除して登録されている
    assert run["tool"]["driver"]["rules"] == [{"id": "F401", "helpUri": "https://docs.astral.sh/ruff/rules/F401/"}]
    # results 配列に diagnostic が載っている
    assert len(run["results"]) == 1
    entry = run["results"][0]
    assert entry["level"] == "error"
    assert entry["message"]["text"] == "unused import"
    assert entry["ruleId"] == "F401"
    assert entry["ruleIndex"] == 0
    loc = entry["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "src/foo.py"
    assert loc["region"]["startLine"] == 10
    # retry_command は invocations に入る
    assert run["invocations"][0]["commandLine"] == "pyfltr run --commands ruff-check -- src/foo.py"
    # executionSuccessful は has_error の反対
    assert run["invocations"][0]["executionSuccessful"] is False
    # pyfltr プロパティにメタ情報
    assert sarif["properties"]["pyfltr"]["run_id"] == "01ABC"
    assert sarif["properties"]["pyfltr"]["exit_code"] == 1


def test_build_sarif_severity_mapping() -> None:
    """severity 3 値が SARIF level に正しくマップされる。"""
    infos = [
        _make_error("tool", "a.py", 1, "e"),
        _make_error("tool", "a.py", 2, "w"),
        _make_error("tool", "a.py", 3, "i"),
    ]
    infos[0].severity = "error"
    infos[1].severity = "warning"
    infos[2].severity = "info"
    result = _make_result("tool", returncode=1, errors=infos)

    config = pyfltr.config.create_default_config()
    sarif = pyfltr.sarif_output.build_sarif([result], config, exit_code=1, commands=["tool"], files=1)
    levels = [r["level"] for r in sarif["runs"][0]["results"]]
    assert levels == ["error", "warning", "note"]


def test_build_sarif_no_errors() -> None:
    """エラー無しなら results が空配列、rules も空になる。"""
    result = _make_result("mypy", returncode=0)
    config = pyfltr.config.create_default_config()
    sarif = pyfltr.sarif_output.build_sarif([result], config, exit_code=0, commands=["mypy"], files=1)
    run = sarif["runs"][0]
    assert run["results"] == []
    assert run["tool"]["driver"]["rules"] == []
    assert run["invocations"][0]["executionSuccessful"] is True
    # retry_command は失敗時のみ populate されるため、成功時は commandLine が省略される
    assert "commandLine" not in run["invocations"][0]


def test_build_sarif_without_rule_url() -> None:
    """rule_url が無い場合、rules エントリから helpUri が省略される。"""
    errors = [_make_error("tool", "a.py", 1, "x")]
    errors[0].rule = "X1"
    errors[0].severity = "warning"
    result = _make_result("tool", returncode=1, errors=errors)
    config = pyfltr.config.create_default_config()
    sarif = pyfltr.sarif_output.build_sarif([result], config, exit_code=1, commands=["tool"], files=1)
    rules = sarif["runs"][0]["tool"]["driver"]["rules"]
    assert rules == [{"id": "X1"}]
