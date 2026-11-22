import re

import pyfltr.paths
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import json_int, normalize_severity, parse_with_pattern, try_json_loads


def parse_hadolint_json(output: str) -> list[ErrorLocation]:
    data = try_json_loads(output)
    if not isinstance(data, list):
        return []
    results = []
    for item in data:
        if not isinstance(item, dict) or not item.get("file") or not (line := json_int(item.get("line"))):
            continue
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(item["file"])),
                line=line,
                col=json_int(item.get("column")),
                command="hadolint",
                message=str(item.get("message", "")),
                rule=str(item["code"]) if item.get("code") else None,
                severity=normalize_severity(item.get("level")),
            )
        )
    return results


def parse_gitleaks_json(output: str) -> list[ErrorLocation]:
    data = try_json_loads(output)
    if not isinstance(data, list):
        return []
    results = []
    for item in data:
        if not isinstance(item, dict) or not item.get("File") or not (line := json_int(item.get("StartLine"))):
            continue
        rule = str(item.get("RuleID", ""))
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(item["File"])),
                line=line,
                col=json_int(item.get("StartColumn")),
                command="gitleaks",
                message="秘密情報の候補を検出しました",
                rule=rule or None,
                severity="error",
            )
        )
    return results


def parse_oxlint_json(output: str) -> list[ErrorLocation]:
    data = try_json_loads(output)
    if not isinstance(data, dict) or not isinstance(data.get("diagnostics"), list):
        return []
    results = []
    for item in data["diagnostics"]:
        if not isinstance(item, dict) or not item.get("filename"):
            continue
        labels = item.get("labels", [])
        span = next((label.get("span") for label in labels if isinstance(label, dict)), None)
        if not isinstance(span, dict) or not (line := json_int(span.get("line"))):
            continue
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(item["filename"])),
                line=line,
                col=json_int(span.get("column")),
                command="oxlint",
                message=str(item.get("message", "")),
                rule=str(item["code"]) if item.get("code") else None,
                severity=normalize_severity(item.get("severity")),
            )
        )
    return results


_CARGO_SHORT = (
    r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<severity>error|warning)(?:\[(?P<rule>[^\]]+)\])?: (?P<message>.+)$"
)
_CARGO_HEADER = re.compile(r"^(?P<severity>error|warning)(?:\[(?P<rule>[^\]]+)\])?: (?P<message>.+)$")
_CARGO_LOCATION = re.compile(r"^\s*(?:--> |┌─ |├─ )(?P<file>.+?):(?P<line>\d+):(?P<col>\d+)\s*$")
_CARGO_PANIC = re.compile(r"^thread .+ panicked at (?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*$")
_CARGO_LINT_NOTE = re.compile(r"^\s*= note: `#\[(?:warn|deny|forbid)\((?P<rule>[\w:]+)\)\]` .+$")


def _cargo(output: str, command: str) -> list[ErrorLocation]:
    results = parse_with_pattern(command, output, _CARGO_SHORT)
    header: re.Match[str] | None = None
    current: ErrorLocation | None = None
    lines = output.splitlines()
    for index, line in enumerate(lines):
        found = _CARGO_HEADER.fullmatch(line)
        if found is not None:
            header = found
            current = None
            continue
        lint = _CARGO_LINT_NOTE.fullmatch(line)
        if lint is not None and current is not None and current.rule is None:
            current.rule = lint["rule"]
        location = _CARGO_LOCATION.fullmatch(line)
        panic = _CARGO_PANIC.fullmatch(line)
        if panic is not None:
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(panic["file"]),
                    line=int(panic["line"]),
                    col=int(panic["col"]),
                    command=command,
                    message=lines[index + 1].strip() if index + 1 < len(lines) else "panic",
                    severity="error",
                )
            )
        elif location is not None and header is not None:
            file = location["file"]
            if "://" not in file:
                results.append(
                    ErrorLocation(
                        file=pyfltr.paths.normalize_separators(file),
                        line=int(location["line"]),
                        col=int(location["col"]),
                        command=command,
                        message=header["message"],
                        rule=header["rule"],
                        severity=normalize_severity(header["severity"]),
                    )
                )
            header = None
            current = results[-1] if "://" not in file else None
    return results


def parse_cargo_clippy(output: str) -> list[ErrorLocation]:
    return _cargo(output, "cargo-clippy")


def parse_cargo_check(output: str) -> list[ErrorLocation]:
    return _cargo(output, "cargo-check")


def parse_cargo_test(output: str) -> list[ErrorLocation]:
    return _cargo(output, "cargo-test")


def parse_cargo_deny(output: str) -> list[ErrorLocation]:
    return _cargo(output, "cargo-deny")


_DOTNET_BUILD = (
    r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)(?:,(?P<end_line>\d+),(?P<end_col>\d+))?\): "
    r"(?P<severity>error|warning) (?P<rule>[^: ]+): (?P<message>.+?)(?: \[[^\]]+\])?$"
)
_DOTNET_STACK = re.compile(r"^\s+at .+ in (?P<file>.+?):line (?P<line>\d+)\s*$")


def parse_dotnet_build(output: str) -> list[ErrorLocation]:
    results = parse_with_pattern("dotnet-build", output, _DOTNET_BUILD)
    return list({(item.file, item.line, item.col, item.message, item.rule): item for item in results}.values())


def parse_dotnet_test(output: str) -> list[ErrorLocation]:
    results = parse_with_pattern("dotnet-test", output, _DOTNET_BUILD)
    message = "テストが失敗しました"
    in_message = False
    for line in output.splitlines():
        if line.strip() in ("Error Message:", "エラー メッセージ:"):
            in_message = True
            continue
        if in_message and line.strip():
            message = line.strip()
            in_message = False
        match = _DOTNET_STACK.fullmatch(line)
        if match is not None:
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(match["file"]),
                    line=int(match["line"]),
                    col=None,
                    command="dotnet-test",
                    message=message,
                    severity="error",
                )
            )
    return list({(item.file, item.line, item.message): item for item in results}.values())
