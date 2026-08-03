"""sarif_outputのテストコード。"""

import pyfltr.config.config
import pyfltr.output.sarif
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

    config = pyfltr.config.config.create_default_config()
    sarif = pyfltr.output.sarif.build_sarif([result], config, exit_code=1, commands=["ruff-check"], files=1, run_id="01ABC")

    assert sarif["version"] == "2.1.0"
    assert sarif["$schema"].endswith(".json")
    assert len(sarif["runs"]) == 1
    run = sarif["runs"][0]
    assert run["tool"]["driver"]["name"] == "ruff-check"
    # rulesに重複排除して登録されている
    assert run["tool"]["driver"]["rules"] == [{"id": "F401", "helpUri": "https://docs.astral.sh/ruff/rules/F401/"}]
    # results配列にdiagnosticが載っている
    assert len(run["results"]) == 1
    entry = run["results"][0]
    assert entry["level"] == "error"
    assert entry["message"]["text"] == "unused import"
    assert entry["ruleId"] == "F401"
    assert entry["ruleIndex"] == 0
    loc = entry["locations"][0]["physicalLocation"]
    assert loc["artifactLocation"]["uri"] == "src/foo.py"
    assert loc["region"] == {"startLine": 10}
    # retry_commandはinvocationsに入る
    assert run["invocations"][0]["commandLine"] == "pyfltr run --commands ruff-check -- src/foo.py"
    # executionSuccessfulはhas_errorの反対
    assert run["invocations"][0]["executionSuccessful"] is False
    # pyfltrプロパティにメタ情報
    assert sarif["properties"]["pyfltr"]["run_id"] == "01ABC"
    assert sarif["properties"]["pyfltr"]["exit_code"] == 1


def test_build_sarif_severity_mapping() -> None:
    """severity 3値がSARIF levelに正しくマップされる。"""
    infos = [
        _make_error("tool", "a.py", 1, "e"),
        _make_error("tool", "a.py", 2, "w"),
        _make_error("tool", "a.py", 3, "i"),
    ]
    infos[0].severity = "error"
    infos[1].severity = "warning"
    infos[2].severity = "info"
    result = _make_result("tool", returncode=1, errors=infos)

    config = pyfltr.config.config.create_default_config()
    sarif = pyfltr.output.sarif.build_sarif([result], config, exit_code=1, commands=["tool"], files=1)
    levels = [r["level"] for r in sarif["runs"][0]["results"]]
    assert levels == ["error", "warning", "note"]


def test_build_sarif_no_errors() -> None:
    """エラー無しなら results が空配列、rules も空になる。"""
    result = _make_result("mypy", returncode=0)
    config = pyfltr.config.config.create_default_config()
    sarif = pyfltr.output.sarif.build_sarif([result], config, exit_code=0, commands=["mypy"], files=1)
    run = sarif["runs"][0]
    assert run["results"] == []
    assert run["tool"]["driver"]["rules"] == []
    assert run["invocations"][0]["executionSuccessful"] is True
    # retry_commandは失敗時のみpopulateされるため、成功時はcommandLineが省略される
    assert "commandLine" not in run["invocations"][0]


def test_build_sarif_without_rule_url() -> None:
    """rule_urlが無い場合、rulesエントリからhelpUriが省略される。"""
    errors = [_make_error("tool", "a.py", 1, "x")]
    errors[0].rule = "X1"
    errors[0].severity = "warning"
    result = _make_result("tool", returncode=1, errors=errors)
    config = pyfltr.config.config.create_default_config()
    sarif = pyfltr.output.sarif.build_sarif([result], config, exit_code=1, commands=["tool"], files=1)
    rules = sarif["runs"][0]["tool"]["driver"]["rules"]
    assert rules == [{"id": "X1"}]


def test_build_sarif_with_end_position() -> None:
    """開始位置と終端排他の終了位置がregionへ反映される。"""
    error = _make_error("ruff-check", "src/foo.py", 10, "unused import", col=3)
    error.end_line = 11
    error.end_col = 7
    result = _make_result("ruff-check", returncode=1, errors=[error])

    config = pyfltr.config.config.create_default_config()
    sarif = pyfltr.output.sarif.build_sarif([result], config, exit_code=1)

    region = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region == {"startLine": 10, "startColumn": 3, "endLine": 11, "endColumn": 7}


def test_build_sarif_omits_columns_less_than_one() -> None:
    """1未満の開始列・終了列は不明な列として省略される。"""
    error = _make_error("tool", "src/foo.py", 10, "bad position", col=0)
    error.end_line = 10
    error.end_col = 0
    result = _make_result("tool", returncode=1, errors=[error])

    config = pyfltr.config.config.create_default_config()
    sarif = pyfltr.output.sarif.build_sarif([result], config, exit_code=1)

    region = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region == {"startLine": 10, "endLine": 10}


def test_build_sarif_omits_textlint_columns() -> None:
    """textlintの列は行内位置を保証できないため、終了行だけを反映する。"""
    error = _make_error("textlint", "docs/index.md", 1, "word", col=9)
    error.end_line = 2
    error.end_col = 14
    result = _make_result("textlint", returncode=1, errors=[error])

    config = pyfltr.config.config.create_default_config()
    sarif = pyfltr.output.sarif.build_sarif([result], config, exit_code=1)

    region = sarif["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region == {"startLine": 1, "endLine": 2}
