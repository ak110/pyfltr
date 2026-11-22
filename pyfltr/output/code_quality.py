from __future__ import annotations

import hashlib
import typing

import pyfltr.command.core_
import pyfltr.diagnostics
import pyfltr.parsing.entry

_SEVERITY_MAP: dict[str | None, str] = {
    "error": "major",
    "warning": "minor",
    "info": "info",
}
"""pyfltr severityからCode Quality severityへのマップ。未登録キーは`minor`にフォールバック。"""


def build_code_quality_payload(
    results: list[pyfltr.command.core_.CommandResult],
) -> list[dict[str, typing.Any]]:
    payload: list[dict[str, typing.Any]] = []
    for result in results:
        for error in result.errors:
            payload.append(_build_issue(error))
    return payload


def _build_issue(error: pyfltr.diagnostics.ErrorLocation) -> dict[str, typing.Any]:
    check_name = f"{error.command}:{error.rule}" if error.rule else error.command
    begin = error.line if error.line else 1
    lines: dict[str, int] = {"begin": begin}
    if error.end_line is not None and error.end_line >= begin:
        lines["end"] = error.end_line
    return {
        "description": error.message,
        "check_name": check_name,
        "fingerprint": _build_fingerprint(error),
        "severity": _SEVERITY_MAP.get(error.severity, "minor"),
        "location": {
            "path": error.file,
            "lines": lines,
        },
    }


def _build_fingerprint(error: pyfltr.diagnostics.ErrorLocation) -> str:
    parts = [
        error.command,
        error.file,
        str(error.line) if error.line is not None else "",
        str(error.col) if error.col is not None else "",
        error.rule or "",
        error.message,
    ]
    key = "\t".join(parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()
