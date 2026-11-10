import json

import pytest

import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools


def test_parse_uv_audit() -> None:
    """uv auditのテキスト出力から脆弱性を抽出する（複数advisory・stderr由来ノイズ混在）。"""
    output = (
        "warning: `uv audit` is experimental and may change without warning.\n"
        "Found 2 known vulnerabilities and no adverse project statuses in 146 packages\n"
        "\n"
        "Vulnerabilities:\n"
        "\n"
        "starlette 1.0.0 has 1 known vulnerability:\n"
        "\n"
        "- PYSEC-2026-161: Missing Host header validation poisons request.url.path\n"
        "\n"
        "  Fixed in: 1.0.1\n"
        "\n"
        "  Advisory information: https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr\n"
        "\n"
        "requests 2.0.0 has 1 known vulnerability:\n"
        "\n"
        "- GHSA-9hjg-9r4m-mvj7: Session verification bypass\n"
        "\n"
        "  Fixed in: 2.32.0\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("uv-audit", output)
    assert len(errors) == 2
    assert all(e.command == "uv-audit" for e in errors)
    assert all(e.file == "pyproject.toml" for e in errors)
    assert all(e.line == 1 for e in errors)
    assert all(e.severity == "error" for e in errors)
    assert errors[0].rule == "PYSEC-2026-161"
    assert errors[0].message.startswith("starlette 1.0.0: ")
    assert "Missing Host header validation" in errors[0].message
    assert errors[1].rule == "GHSA-9hjg-9r4m-mvj7"
    assert errors[1].message.startswith("requests 2.0.0: ")


def test_parse_uv_audit_advisory_without_package_header() -> None:
    """package見出し行が先行しないadvisory行は説明のみをmessageへ格納する（フォールバック分岐）。"""
    output = "- PYSEC-2026-999: Some isolated advisory\n"
    errors = pyfltr.parsing.entry.parse_errors("uv-audit", output)
    assert len(errors) == 1
    assert errors[0].command == "uv-audit"
    assert errors[0].file == "pyproject.toml"
    assert errors[0].line == 1
    assert errors[0].rule == "PYSEC-2026-999"
    # package見出しが無いためパッケージ名の前置きは付かない。
    assert errors[0].message == "Some isolated advisory"


def test_parse_uv_audit_same_id_across_packages_not_deduplicated() -> None:
    """同一advisory IDが別パッケージ見出し配下に出た場合、別診断として両方保持する（重複排除しない）。"""
    output = (
        "starlette 1.0.0 has 1 known vulnerability:\n"
        "\n"
        "- GHSA-aaaa-bbbb-cccc: Shared transitive advisory\n"
        "\n"
        "requests 2.0.0 has 1 known vulnerability:\n"
        "\n"
        "- GHSA-aaaa-bbbb-cccc: Shared transitive advisory\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("uv-audit", output)
    # パッケージ単位で列挙されるため同一IDでも2件保持する。
    assert len(errors) == 2
    assert all(e.rule == "GHSA-aaaa-bbbb-cccc" for e in errors)
    assert errors[0].message.startswith("starlette 1.0.0: ")
    assert errors[1].message.startswith("requests 2.0.0: ")


def test_parse_uv_audit_no_advisories() -> None:
    """脆弱性なし出力（Found 0行のみ）・空文字・advisory非該当テキストはいずれも空リストを返す。"""
    found_zero = (
        "warning: `uv audit` is experimental and may change without warning.\n"
        "Found 0 known vulnerabilities and no adverse project statuses in 146 packages\n"
    )
    assert pyfltr.parsing.entry.parse_errors("uv-audit", found_zero) == []
    assert pyfltr.parsing.entry.parse_errors("uv-audit", "") == []
    assert pyfltr.parsing.entry.parse_errors("uv-audit", "no relevant lines here") == []


def test_parse_npm_audit_json() -> None:
    """npm audit --json（auditReportVersion 2）でvia文字列要素のスキップとsource重複排除を確認する。"""
    output = json.dumps(
        {
            "auditReportVersion": 2,
            "vulnerabilities": {
                "minimist": {
                    "name": "minimist",
                    "severity": "critical",
                    "via": [
                        {
                            "source": 1096466,
                            "name": "minimist",
                            "title": "Prototype Pollution in minimist",
                            "url": "https://github.com/advisories/GHSA-vh95-rmgr-6w4m",
                            "severity": "moderate",
                            "range": "<0.2.1",
                        },
                        {
                            "source": 1097677,
                            "name": "minimist",
                            "title": "Prototype Pollution in minimist",
                            "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                            "severity": "critical",
                            "range": "<0.2.4",
                        },
                        "another-package",
                    ],
                    "range": "<=0.2.3",
                },
                "another-package": {
                    "name": "another-package",
                    "severity": "critical",
                    "via": [
                        {
                            "source": 1097677,
                            "name": "minimist",
                            "title": "Prototype Pollution in minimist",
                            "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                            "severity": "critical",
                            "range": "<0.2.4",
                        }
                    ],
                },
            },
            "metadata": {"vulnerabilities": {"total": 2}},
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("npm-audit", output)
    # 文字列要素スキップ・source重複排除によりsource 1096466 / 1097677の2件のみ。
    assert len(errors) == 2
    assert all(e.command == "npm-audit" for e in errors)
    assert all(e.file == "package.json" for e in errors)
    assert all(e.line == 1 for e in errors)
    by_rule = {e.rule: e for e in errors}
    assert set(by_rule) == {"GHSA-vh95-rmgr-6w4m", "GHSA-xvch-5gv4-984h"}
    assert by_rule["GHSA-vh95-rmgr-6w4m"].severity == "warning"  # moderate
    assert by_rule["GHSA-xvch-5gv4-984h"].severity == "error"  # critical
    assert "minimist" in by_rule["GHSA-vh95-rmgr-6w4m"].message
    assert "(<0.2.1)" in by_rule["GHSA-vh95-rmgr-6w4m"].message
    assert by_rule["GHSA-xvch-5gv4-984h"].rule_url == "https://github.com/advisories/GHSA-xvch-5gv4-984h"


def test_parse_pnpm_audit_json() -> None:
    """pnpm audit --json（advisories形式）から脆弱性を抽出する。"""
    output = json.dumps(
        {
            "advisories": {
                "1096466": {
                    "id": 1096466,
                    "title": "Prototype Pollution in minimist",
                    "module_name": "minimist",
                    "severity": "moderate",
                    "vulnerable_versions": "<0.2.1",
                    "github_advisory_id": "GHSA-vh95-rmgr-6w4m",
                    "url": "https://github.com/advisories/GHSA-vh95-rmgr-6w4m",
                },
                "1097677": {
                    "id": 1097677,
                    "title": "Prototype Pollution in minimist",
                    "module_name": "minimist",
                    "severity": "critical",
                    "vulnerable_versions": "<0.2.4",
                    "github_advisory_id": "GHSA-xvch-5gv4-984h",
                    "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                },
            },
            "metadata": {"vulnerabilities": {"moderate": 1, "critical": 1}},
        }
    )
    errors = pyfltr.parsing.entry.parse_errors("pnpm-audit", output)
    assert len(errors) == 2
    assert all(e.command == "pnpm-audit" for e in errors)
    assert all(e.file == "package.json" for e in errors)
    assert errors[0].rule == "GHSA-vh95-rmgr-6w4m"
    assert errors[0].severity == "warning"
    assert errors[0].message.startswith("minimist: ")
    assert "(<0.2.1)" in errors[0].message
    assert errors[1].rule == "GHSA-xvch-5gv4-984h"
    assert errors[1].severity == "error"
    assert errors[1].rule_url == "https://github.com/advisories/GHSA-xvch-5gv4-984h"


def test_parse_yarn_audit_jsonl() -> None:
    """yarn audit --json（JSON Lines）でauditAdvisory抽出・id重複排除・summary行スキップを確認する。"""
    lines = [
        json.dumps(
            {
                "type": "auditAdvisory",
                "data": {
                    "advisory": {
                        "id": 1096466,
                        "title": "Prototype Pollution in minimist",
                        "module_name": "minimist",
                        "severity": "moderate",
                        "vulnerable_versions": "<0.2.1",
                        "github_advisory_id": "GHSA-vh95-rmgr-6w4m",
                        "url": "https://github.com/advisories/GHSA-vh95-rmgr-6w4m",
                    }
                },
            }
        ),
        json.dumps(
            {
                "type": "auditAdvisory",
                "data": {
                    "advisory": {
                        "id": 1097677,
                        "title": "Prototype Pollution in minimist",
                        "module_name": "minimist",
                        "severity": "critical",
                        "vulnerable_versions": "<0.2.4",
                        "github_advisory_id": "GHSA-xvch-5gv4-984h",
                        "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                    }
                },
            }
        ),
        # 同一advisory（id重複）→ 重複排除される。
        json.dumps(
            {
                "type": "auditAdvisory",
                "data": {
                    "advisory": {
                        "id": 1097677,
                        "title": "Prototype Pollution in minimist",
                        "module_name": "minimist",
                        "severity": "critical",
                        "vulnerable_versions": "<0.2.4",
                        "github_advisory_id": "GHSA-xvch-5gv4-984h",
                        "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                    }
                },
            }
        ),
        # auditSummary行は集計のためスキップされる。
        json.dumps({"type": "auditSummary", "data": {"vulnerabilities": {"moderate": 1, "critical": 1}}}),
    ]
    errors = pyfltr.parsing.entry.parse_errors("yarn-audit", "\n".join(lines))
    assert len(errors) == 2
    assert all(e.command == "yarn-audit" for e in errors)
    assert all(e.file == "package.json" for e in errors)
    assert errors[0].rule == "GHSA-vh95-rmgr-6w4m"
    assert errors[0].severity == "warning"
    assert errors[1].rule == "GHSA-xvch-5gv4-984h"
    assert errors[1].severity == "error"
    assert errors[1].message.startswith("minimist: ")


@pytest.mark.parametrize(
    "command,empty_output",
    [
        ("npm-audit", json.dumps({"auditReportVersion": 2, "vulnerabilities": {}, "metadata": {}})),
        ("pnpm-audit", json.dumps({"advisories": {}, "metadata": {}})),
        ("yarn-audit", json.dumps({"type": "auditSummary", "data": {"vulnerabilities": {}}})),
    ],
)
def test_parse_js_audit_no_vulnerabilities(command: str, empty_output: str) -> None:
    """脆弱性なしのJSON出力ではJavaScript系監査ツールは空リストを返す（uv-auditはテキストのため別テスト）。"""
    assert pyfltr.parsing.entry.parse_errors(command, empty_output) == []


@pytest.mark.parametrize("command", ["npm-audit", "pnpm-audit", "yarn-audit"])
def test_parse_js_audit_invalid_input(command: str) -> None:
    """不正JSON・空文字ではJavaScript系監査ツールは空リストを返す（uv-auditはテキストのため別テスト）。"""
    assert pyfltr.parsing.entry.parse_errors(command, "not json") == []
    assert pyfltr.parsing.entry.parse_errors(command, "") == []


@pytest.mark.parametrize(
    ("command", "expected"),
    [("npm-audit", "sample (<2)"), ("pnpm-audit", "sample:  (<2)"), ("yarn-audit", "sample:  (<2)")],
)
def test_audit_empty_title_preserves_tool_prefix(command: str, expected: str) -> None:
    """タイトルが空の場合もツールごとの接頭辞と版範囲を保つ。"""
    advisory = {"module_name": "sample", "name": "sample", "title": "", "range": "<2", "vulnerable_versions": "<2"}
    if command == "npm-audit":
        data = {"vulnerabilities": {"sample": {"via": [advisory]}}}
    elif command == "pnpm-audit":
        data = {"advisories": {"1": advisory}}
    else:
        data = {"type": "auditAdvisory", "data": {"advisory": advisory}}
    errors = pyfltr.parsing.entry.parse_errors(command, json.dumps(data))
    assert [(error.file, error.line, error.message) for error in errors] == [("package.json", 1, expected)]
