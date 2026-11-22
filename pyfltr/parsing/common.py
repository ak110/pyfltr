import enum
import json
import pathlib
import re
import typing

import pyfltr.paths
import pyfltr.rule_urls
from pyfltr.diagnostics import ErrorLocation


class ErrorPatternGroup(enum.StrEnum):
    FILE = "file"
    LINE = "line"
    MESSAGE = "message"
    COL = "col"
    END_LINE = "end_line"
    END_COL = "end_col"
    RULE = "rule"
    SEVERITY = "severity"


REQUIRED_ERROR_PATTERN_GROUPS = (ErrorPatternGroup.FILE, ErrorPatternGroup.LINE, ErrorPatternGroup.MESSAGE)
OPTIONAL_ERROR_PATTERN_GROUPS = tuple(group for group in ErrorPatternGroup if group not in REQUIRED_ERROR_PATTERN_GROUPS)


def try_json_loads(output: str) -> typing.Any:
    stripped = output.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    candidates: list[tuple[int, typing.Any]] = []
    for match in re.finditer(r"[\{\[]", stripped):
        try:
            value, end_index = decoder.raw_decode(stripped[match.start() :])
        except json.JSONDecodeError:
            continue
        candidates.append((match.start() + end_index, value))
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[1]


def json_list_field(output: str, field: str) -> list[typing.Any] | None:
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return None
    value = data.get(field, [])
    return value if isinstance(value, list) else None


def normalize_severity(value: typing.Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return eslint_severity(value)
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if not lowered:
        return None
    if lowered in ("error", "fatal", "high"):
        return "error"
    if lowered in ("warning", "warn", "medium"):
        return "warning"
    if lowered in ("info", "information", "informational", "note", "notice", "hint", "style", "convention", "refactor", "low"):
        return "info"
    return None


def to_inclusive_end_position(line: int, end_line: int | None, end_col: int | None) -> tuple[int | None, int | None]:
    if end_col == 1 and end_line is not None and end_line > line:
        return end_line - 1, None
    return end_line, end_col


def eslint_severity(value: typing.Any) -> str | None:
    if value == 2:
        return "error"
    if value == 1:
        return "warning"
    return None


def json_int(value: typing.Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def one_based_offset(value: typing.Any) -> int | None:
    offset = json_int(value)
    return offset + 1 if offset is not None and offset >= 0 else None


def parse_file_messages_format(
    data: list[dict],
    message_to_location: typing.Callable[[dict, str], ErrorLocation | None],
    *,
    file_key: str = "filePath",
    messages_key: str = "messages",
    file_to_string: typing.Callable[[typing.Any], str] = str,
) -> list[ErrorLocation]:
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        file_path = file_to_string(entry.get(file_key, ""))
        messages = entry.get(messages_key, [])
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            location = message_to_location(msg, file_path)
            if location is not None:
                results.append(location)
    return results


def parse_optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_with_pattern(command: str, output: str, pattern: str) -> list[ErrorLocation]:
    compiled = re.compile(pattern)
    results: list[ErrorLocation] = []
    for line in output.splitlines():
        match = compiled.search(line)
        if match is None:
            continue
        groups = match.groupdict()
        file_path = groups.get(ErrorPatternGroup.FILE, "")
        line_str = groups.get(ErrorPatternGroup.LINE, "0")
        message = groups.get(ErrorPatternGroup.MESSAGE) or ""
        try:
            line_num = int(line_str)
        except ValueError:
            continue
        col_num = parse_optional_int(groups.get(ErrorPatternGroup.COL))
        end_line_num = parse_optional_int(groups.get(ErrorPatternGroup.END_LINE))
        end_col_num = parse_optional_int(groups.get(ErrorPatternGroup.END_COL))
        rule_raw = groups.get(ErrorPatternGroup.RULE)
        rule = rule_raw.strip() if isinstance(rule_raw, str) and rule_raw.strip() else None
        rule_url = pyfltr.rule_urls.build_rule_url(command, rule) if rule is not None else None
        severity = normalize_severity(groups.get(ErrorPatternGroup.SEVERITY))
        end_line_num, end_col_num = to_inclusive_end_position(line_num, end_line_num, end_col_num)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(file_path),
                line=line_num,
                col=col_num,
                command=command,
                message=message.strip(),
                rule=rule,
                severity=severity,
                rule_url=rule_url,
                end_line=end_line_num,
                end_col=end_col_num,
            )
        )
    return results


def extract_last_line(output: str) -> str | None:
    stripped = output.strip()
    if not stripped or stripped[0] in ("[", "{"):
        return None
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line and not re.fullmatch(r"[=\-*#]+", line):
            return line
    return None


def is_project_path(normalized_path: str) -> bool:
    if pathlib.PurePosixPath(normalized_path).is_absolute():
        return False
    if normalized_path.startswith(".."):
        return False
    if normalized_path.startswith(".venv/"):
        return False
    return not ("site-packages/" in normalized_path or "dist-packages/" in normalized_path)
