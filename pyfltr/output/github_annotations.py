from __future__ import annotations

import typing

import pyfltr.diagnostics
import pyfltr.output.positions

if typing.TYPE_CHECKING:
    import pyfltr.parsing.entry

_SEVERITY_TO_KIND: dict[str | None, str] = {
    "error": "error",
    "warning": "warning",
    "info": "notice",
}


def build_workflow_command(error: pyfltr.diagnostics.ErrorLocation) -> str:
    kind = _SEVERITY_TO_KIND.get(error.severity, "warning")
    props = [f"file={_escape_property(error.file)}", f"line={error.line}"]
    if error.end_line is not None:
        props.append(f"endLine={error.end_line}")
    if pyfltr.output.positions.is_publishable_column(error, error.col):
        props.append(f"col={error.col}")
    if pyfltr.output.positions.is_publishable_column(error, error.end_col):
        props.append(f"endColumn={error.end_col}")
    title = f"{error.command}: {error.rule}" if error.rule else error.command
    props.append(f"title={_escape_property(title)}")
    prefix = _build_plain_prefix(error)
    message_text = _escape_message(f"{prefix} {error.message}")
    return f"::{kind} {','.join(props)}::{message_text}"


def _build_plain_prefix(error: pyfltr.diagnostics.ErrorLocation) -> str:
    location = error.file or "?"
    if error.line:
        location += f":{error.line}"
    if error.col is not None:
        location += f":{error.col}"
    tag = f"[{error.command}:{error.rule}]" if error.rule else f"[{error.command}]"
    return f"{location}: {tag}"


def _escape_property(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C")


def _escape_message(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
