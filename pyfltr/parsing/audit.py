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
    if not isinstance(value, str):
        return None
    return AUDIT_SEVERITY_MAP.get(value.strip().lower())


GHSA_RE = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}", re.IGNORECASE)


def extract_advisory_rule(url: str, fallback_id: typing.Any) -> str | None:
    match = GHSA_RE.search(url)
    if match is not None:
        return match.group(0)
    if fallback_id is not None:
        return str(fallback_id)
    return None


UV_AUDIT_PACKAGE_RE = re.compile(r"^(?P<pkg>\S+)\s+(?P<version>\S+)\s+has\s+\d+\s+known\s+(?:vulnerability|vulnerabilities)\b")


UV_AUDIT_ADVISORY_RE = re.compile(r"^-\s+(?P<id>\S+):\s+(?P<message>.+)$")


def parse_uv_audit(output: str) -> list[ErrorLocation]:
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
                continue
            url = str(via.get("url", "") or "")
            title = str(via.get("title", "") or "")
            source = via.get("source")
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
