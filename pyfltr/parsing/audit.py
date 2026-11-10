"""依存の脆弱性監査の出力解析。"""

import json
import re
import typing

from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import (
    try_json_loads,
)

AUDIT_SEVERITY_MAP: dict[str, str] = {
    "critical": "error",
    "high": "error",
    "moderate": "warning",
    "low": "warning",
    "info": "info",
}


def normalize_audit_severity(value: typing.Any) -> str | None:
    """npm系auditツールの深刻度ラベルを`"error"` / `"warning"` / `"info"`へ正規化する。

    未知の値やNoneは`None`を返し、JSONL出力側で省略される。
    """
    if not isinstance(value, str):
        return None
    return AUDIT_SEVERITY_MAP.get(value.strip().lower())


GHSA_RE = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}", re.IGNORECASE)


def extract_advisory_rule(url: str, fallback_id: typing.Any) -> str | None:
    """Advisory URLからGHSA識別子を抽出する。無ければ`fallback_id`を文字列化して返す。

    npm系auditツールはadvisory URLにGHSA識別子を含むため、機械判別可能なruleとして採用する。
    URLに含まれない場合はadvisoryの数値ID（`fallback_id`）へフォールバックし、いずれも無ければ`None`。
    """
    match = GHSA_RE.search(url)
    if match is not None:
        return match.group(0)
    if fallback_id is not None:
        return str(fallback_id)
    return None


UV_AUDIT_PACKAGE_RE = re.compile(r"^(?P<pkg>\S+)\s+(?P<version>\S+)\s+has\s+\d+\s+known\s+(?:vulnerability|vulnerabilities)\b")


UV_AUDIT_ADVISORY_RE = re.compile(r"^-\s+(?P<id>\S+):\s+(?P<message>.+)$")


def parse_uv_audit(output: str) -> list[ErrorLocation]:
    """`uv audit`のテキスト出力をパースする。

    uvは機械可読出力（JSON等）の指定フラグを持たないためテキストを解析する。
    出力例（stderrの実験的警告・サマリーがpyfltr側でstdout統合され混在し得るが、脆弱性本体は次の形）::

        Vulnerabilities:

        starlette 1.0.0 has 1 known vulnerability:

        - PYSEC-2026-161: Missing Host header validation poisons request.url.path ...

          Fixed in: 1.0.1

          Advisory information: https://github.com/Kludex/starlette/security/advisories/GHSA-...

    `<pkg> <version> has N known vulnerabilities`行（単数時は vulnerability）で対象パッケージを把握し、
    続く`- <ID>: <説明>`行を1件の`ErrorLocation`へ変換する。`Fixed in:`等のインデント行は`- `で始まらないため対象外。
    uv auditは行情報を持たないため、`lychee`に倣いマニフェスト`pyproject.toml`を`file`、`line=1`固定とする。
    テキスト出力では深刻度を分類しないため`severity="error"`固定とする。
    uvはパッケージ見出し単位で各advisoryを1回ずつ列挙する（別パッケージ経由の同一IDは別診断として扱う）ため、
    JSON系3パーサーのような重複排除は行わない。
    """
    results: list[ErrorLocation] = []
    current_package = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        package_match = UV_AUDIT_PACKAGE_RE.match(line)
        if package_match is not None:
            current_package = f"{package_match.group('pkg')} {package_match.group('version')}"
            continue
        advisory_match = UV_AUDIT_ADVISORY_RE.match(line)
        if advisory_match is None:
            continue
        description = advisory_match.group("message").strip()
        message = f"{current_package}: {description}" if current_package else description
        results.append(
            ErrorLocation(
                file="pyproject.toml",
                line=1,
                col=None,
                command="uv-audit",
                message=message,
                rule=advisory_match.group("id"),
                severity="error",
            )
        )
    return results


def parse_npm_audit_json(output: str) -> list[ErrorLocation]:
    """`npm audit --json`出力（auditReportVersion 2形式）をパースする。

    出力例::

        {
          "auditReportVersion": 2,
          "vulnerabilities": {
            "minimist": {
              "name": "minimist", "severity": "critical",
              "via": [
                {"source": 1097677, "title": "Prototype Pollution in minimist",
                 "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                 "severity": "critical", "range": "<0.2.4"},
                "other-package"
              ]
            }
          },
          "metadata": {...}
        }

    `vulnerabilities.<pkg>.via[]`のうち辞書要素のみが実advisoryで、文字列要素は他パッケージへの
    参照のためスキップする。同一advisory（`source`一致）が複数パッケージのviaに現れ得るため重複排除する。
    行情報を持たないためマニフェスト`package.json`を`file`、`line=1`固定とする。JSON解析失敗時は空リスト。
    """
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return []
    vulnerabilities = data.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        return []
    results: list[ErrorLocation] = []
    seen: set[typing.Any] = set()
    for pkg_name, vuln in vulnerabilities.items():
        if not isinstance(vuln, dict):
            continue
        via_list = vuln.get("via", [])
        if not isinstance(via_list, list):
            continue
        for via in via_list:
            if not isinstance(via, dict):
                continue  # 文字列要素は他パッケージへの参照のためスキップ
            url = str(via.get("url", "") or "")
            title = str(via.get("title", "") or "")
            source = via.get("source")
            # 同一advisory（source一致）が複数パッケージのviaに現れ得るためsource単位で重複排除する。
            # sourceを持たない異常エントリは空キーへの衝突で誤集約しないよう重複排除対象から外し、各件出力する。
            if source is not None:
                if source in seen:
                    continue
                seen.add(source)
            name = str(via.get("name") or pkg_name)
            version_range = str(via.get("range", "") or "")
            message = f"{name}: {title}" if title else name
            results.append(
                _build_audit_diagnostic(
                    command="npm-audit",
                    message=message,
                    version_range=version_range,
                    rule=extract_advisory_rule(url, source),
                    severity=normalize_audit_severity(via.get("severity")),
                    url=url,
                )
            )
    return results


def parse_pnpm_audit_json(output: str) -> list[ErrorLocation]:
    """`pnpm audit --json`出力（advisories形式）をパースする。

    出力例::

        {
          "advisories": {
            "1097677": {
              "id": 1097677, "title": "Prototype Pollution in minimist",
              "module_name": "minimist", "severity": "critical",
              "vulnerable_versions": "<0.2.4",
              "github_advisory_id": "GHSA-xvch-5gv4-984h",
              "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h"
            }
          },
          "metadata": {...}
        }

    `advisories`はadvisory ID → advisory本体のmap。各advisoryから対象モジュール・タイトル・
    深刻度・URLを抽出する。行情報を持たないためマニフェスト`package.json`を`file`、`line=1`固定とする。
    JSON解析失敗時は空リストを返す。
    """
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return []
    advisories = data.get("advisories")
    if not isinstance(advisories, dict):
        return []
    results: list[ErrorLocation] = []
    for advisory in advisories.values():
        if not isinstance(advisory, dict):
            continue
        module = str(advisory.get("module_name", "") or "")
        title = str(advisory.get("title", "") or "")
        version_range = str(advisory.get("vulnerable_versions", "") or "")
        url = str(advisory.get("url", "") or "")
        ghsa = str(advisory.get("github_advisory_id", "") or "")
        message = f"{module}: {title}" if module else title
        results.append(
            _build_audit_diagnostic(
                command="pnpm-audit",
                message=message,
                version_range=version_range,
                rule=ghsa or extract_advisory_rule(url, advisory.get("id")),
                severity=normalize_audit_severity(advisory.get("severity")),
                url=url,
            )
        )
    return results


def parse_yarn_audit_jsonl(output: str) -> list[ErrorLocation]:
    """`yarn audit --json`出力（JSON Lines形式）をパースする。

    yarn classic（1.x）は1行1JSONで出力し、`type == "auditAdvisory"`の行に脆弱性情報、
    `type == "auditSummary"`の行に件数集計を持つ。`ErrorLocation`へ変換する対象は`auditAdvisory`行のみ。

    出力例（1行分）::

        {"type": "auditAdvisory", "data": {"advisory": {
          "id": 1097677, "title": "Prototype Pollution in minimist",
          "module_name": "minimist", "severity": "critical",
          "vulnerable_versions": "<0.2.4",
          "github_advisory_id": "GHSA-xvch-5gv4-984h",
          "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h"}}}

    同一advisory（`id`一致）が依存経路ごとに複数行で現れ得るため重複排除する。
    行情報を持たないためマニフェスト`package.json`を`file`、`line=1`固定とする。解析できない行はスキップする。
    """
    results: list[ErrorLocation] = []
    seen: set[typing.Any] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "auditAdvisory":
            continue
        data = entry.get("data")
        advisory = data.get("advisory") if isinstance(data, dict) else None
        if not isinstance(advisory, dict):
            continue
        url = str(advisory.get("url", "") or "")
        ghsa = str(advisory.get("github_advisory_id", "") or "")
        advisory_id = advisory.get("id")
        dedup_key = advisory_id if advisory_id is not None else (ghsa or url)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        module = str(advisory.get("module_name", "") or "")
        title = str(advisory.get("title", "") or "")
        version_range = str(advisory.get("vulnerable_versions", "") or "")
        message = f"{module}: {title}" if module else title
        results.append(
            _build_audit_diagnostic(
                command="yarn-audit",
                message=message,
                version_range=version_range,
                rule=ghsa or extract_advisory_rule(url, advisory_id),
                severity=normalize_audit_severity(advisory.get("severity")),
                url=url,
            )
        )
    return results


def _build_audit_diagnostic(
    *, command: str, message: str, version_range: str, rule: str | None, severity: str | None, url: str
) -> ErrorLocation:
    """各監査の接頭辞を保ち、版範囲とマニフェスト位置を持つ診断を組み立てる。"""
    if version_range:
        message = f"{message} ({version_range})"
    return ErrorLocation(
        file="package.json",
        line=1,
        col=None,
        command=command,
        message=message,
        rule=rule,
        severity=severity,
        rule_url=url or None,
    )
