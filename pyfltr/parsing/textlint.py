import typing

import pyfltr.paths
import pyfltr.tools
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import (
    normalize_severity,
    parse_file_messages_format,
    parse_with_pattern,
    to_inclusive_end_position,
    try_json_loads,
)


def parse_textlint_json(output: str) -> list[ErrorLocation]:
    data = try_json_loads(output)
    if not isinstance(data, list):
        return parse_with_pattern("textlint", output, pyfltr.tools.BUILTIN_COMMANDS["textlint"].require_diagnostic_pattern())

    def _msg_to_location(msg: dict, file_path: str) -> ErrorLocation | None:
        line = msg.get("line")
        if not isinstance(line, int):
            return None
        raw_col = msg.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        rule_id = str(msg.get("ruleId") or "")
        fix_value = "safe" if msg.get("fix") else "none"
        rule = rule_id or None
        hint = pyfltr.tools.BUILTIN_COMMANDS["textlint"].rule_hints.get(rule_id) if rule_id else None
        message = normalize_whitespace(str(msg.get("message", "")))
        end_line, end_col = extract_textlint_end_position(msg.get("loc"))
        if rule_id == "ja-technical-writing/sentence-length":
            range_text = format_textlint_loc(msg.get("loc"))
            if range_text:
                message = f"{message} {range_text}"
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        return ErrorLocation(
            file=pyfltr.paths.normalize_separators(file_path),
            line=line,
            col=col,
            command="textlint",
            message=message,
            rule=rule,
            severity=normalize_severity(msg.get("severity")),
            fix=fix_value,
            hint=hint,
            end_line=end_line,
            end_col=end_col,
        )

    return parse_file_messages_format(data, _msg_to_location)


def extract_textlint_loc_positions(
    loc: typing.Any,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    if not isinstance(loc, dict):
        return None, None
    return extract_textlint_point(loc.get("start")), extract_textlint_point(loc.get("end"))


def extract_textlint_point(point: typing.Any) -> tuple[int, int] | None:
    if not isinstance(point, dict):
        return None
    line = point.get("line")
    col = point.get("column")
    if not isinstance(line, int) or not isinstance(col, int):
        return None
    return line, col


def extract_textlint_end_position(loc: typing.Any) -> tuple[int | None, int | None]:
    _, end = extract_textlint_loc_positions(loc)
    if end is None:
        return None, None
    return end


def format_textlint_loc(loc: typing.Any) -> str:
    start, end = extract_textlint_loc_positions(loc)
    if start is None or end is None:
        return ""
    start_line, start_col = start
    end_line, end_col = end
    if start_line == end_line:
        return f"(L{start_line}:{start_col}〜{end_col})"
    return f"(L{start_line}:{start_col}〜L{end_line}:{end_col})"


def normalize_whitespace(text: str) -> str:
    return " ".join(text.split())
