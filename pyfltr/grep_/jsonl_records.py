from __future__ import annotations

import pathlib
import typing

import pyfltr.output.jsonl_emit
import pyfltr.output.jsonl_records
import pyfltr.paths
from pyfltr.grep_.preview import MatchPreview
from pyfltr.grep_.types import MatchRecord, ReplaceRecord


def emit_warning(entry: dict[str, typing.Any]) -> None:
    _emit(pyfltr.output.jsonl_records.build_warning_record(entry))


def emit_grep_header(
    *,
    pattern: str,
    files: int,
    run_id: str | None = None,
    format_source: str | None = None,
    output_mode: str | None = None,
) -> None:
    record: dict[str, typing.Any] = {
        "kind": "header",
        "subcommand": "grep",
        "pattern": pattern,
        "files": files,
    }
    if run_id is not None:
        record["run_id"] = run_id
    if format_source is not None:
        record["format_source"] = format_source
    if output_mode is not None:
        record["output_mode"] = output_mode
    _emit(record)


def emit_records(records: typing.Iterable[dict[str, typing.Any]]) -> None:
    pyfltr.output.jsonl_emit.emit_records(pyfltr.output.jsonl_records.dump_record(record) for record in records)


def emit_match(record: MatchRecord, preview: MatchPreview) -> None:
    _emit({"kind": "match", **match_payload(record, preview)})


def match_payload(record: MatchRecord, preview: MatchPreview) -> dict[str, typing.Any]:
    payload: dict[str, typing.Any] = {
        "file": pyfltr.paths.normalize_separators(str(record.file)),
        "line": record.line,
        "col": record.col,
        "match_text": preview.match_text,
        "line_text": preview.line_text,
    }
    if record.end_col is not None:
        payload["end_col"] = record.end_col
    if preview.before_lines:
        payload["before"] = list(preview.before_lines)
    if preview.after_lines:
        payload["after"] = list(preview.after_lines)
    if preview.line_text_offset != 0:
        payload["line_text_offset"] = preview.line_text_offset
    if preview.truncated_fields:
        payload["truncated"] = list(preview.truncated_fields)
    return payload


def emit_grep_summary(
    *,
    total_matches: int,
    files_scanned: int,
    exit_code: int,
    guidance: list[str] | None = None,
    fully_excluded_files: list[str] | None = None,
    missing_targets: list[str] | None = None,
    warning_count: int = 0,
    files_with_matches: int | None = None,
    returned_matches: int | None = None,
    omitted_matches: int | None = None,
    omitted_files: int | None = None,
    output_mode: str | None = None,
) -> None:
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "subcommand": "grep",
        "exit": exit_code,
        "total_matches": total_matches,
        "files_scanned": files_scanned,
    }
    if files_with_matches is not None:
        record["files_with_matches"] = files_with_matches
    if returned_matches is not None:
        record["returned_matches"] = returned_matches
    if omitted_matches is not None:
        record["omitted_matches"] = omitted_matches
    if omitted_files is not None:
        record["omitted_files"] = omitted_files
    if output_mode is not None:
        record["output_mode"] = output_mode
    if warning_count > 0:
        record["warnings"] = warning_count
    if guidance:
        record["guidance"] = list(guidance)
    if fully_excluded_files:
        record["fully_excluded_files"] = list(fully_excluded_files)
    if missing_targets:
        record["missing_targets"] = list(missing_targets)
    _emit(record)


def emit_replace_header(
    *,
    pattern: str,
    replacement: str,
    files: int,
    replace_id: str | None,
    dry_run: bool,
    format_source: str | None = None,
) -> None:
    record: dict[str, typing.Any] = {
        "kind": "header",
        "subcommand": "replace",
        "pattern": pattern,
        "replacement": replacement,
        "files": files,
        "dry_run": dry_run,
    }
    if replace_id is not None:
        record["replace_id"] = replace_id
    if format_source is not None:
        record["format_source"] = format_source
    _emit(record)


def emit_file_change(
    *,
    file: pathlib.Path,
    count: int,
    before_hash: str | None,
    after_hash: str | None,
    dry_run: bool,
    records: list[ReplaceRecord] | None = None,
    show_changes: bool = False,
) -> None:
    payload: dict[str, typing.Any] = {
        "kind": "file_change",
        "file": pyfltr.paths.normalize_separators(str(file)),
        "count": count,
        "dry_run": dry_run,
    }
    if before_hash is not None:
        payload["before_hash"] = before_hash
    if after_hash is not None:
        payload["after_hash"] = after_hash
    if show_changes and records:
        payload["changes"] = [
            {
                "line": r.line,
                "col": r.col,
                "before_line": r.before_line,
                "after_line": r.after_line,
            }
            for r in records
        ]
    _emit(payload)


def emit_replace_summary(
    *,
    files_changed: int,
    total_replacements: int,
    exit_code: int,
    replace_id: str | None,
    dry_run: bool,
    guidance: list[str] | None = None,
    fully_excluded_files: list[str] | None = None,
    missing_targets: list[str] | None = None,
    warning_count: int = 0,
) -> None:
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "subcommand": "replace",
        "exit": exit_code,
        "files_changed": files_changed,
        "total_replacements": total_replacements,
        "dry_run": dry_run,
    }
    if replace_id is not None:
        record["replace_id"] = replace_id
    if warning_count > 0:
        record["warnings"] = warning_count
    if guidance:
        record["guidance"] = list(guidance)
    if fully_excluded_files:
        record["fully_excluded_files"] = list(fully_excluded_files)
    if missing_targets:
        record["missing_targets"] = list(missing_targets)
    _emit(record)


def emit_file_with_matches(file: pathlib.Path, count: int) -> None:
    _emit(
        {
            "kind": "file_with_matches",
            "file": pyfltr.paths.normalize_separators(str(file)),
            "count": count,
        }
    )


def emit_file_without_match(file: pathlib.Path) -> None:
    _emit({"kind": "file_without_match", "file": pyfltr.paths.normalize_separators(str(file))})


def emit_file_count(file: pathlib.Path, count: int) -> None:
    _emit(
        {
            "kind": "file_count",
            "file": pyfltr.paths.normalize_separators(str(file)),
            "count": count,
        }
    )


def emit_replace_history(meta: dict[str, typing.Any]) -> None:
    payload = {"kind": "replace_history", **meta}
    _emit(payload)


def emit_replace_undo_summary(
    *,
    replace_id: str,
    restored: list[pathlib.Path],
    skipped: list[pathlib.Path],
    exit_code: int,
) -> None:
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "subcommand": "replace_undo",
        "exit": exit_code,
        "replace_id": replace_id,
        "restored": [pyfltr.paths.normalize_separators(str(p)) for p in restored],
        "skipped": [pyfltr.paths.normalize_separators(str(p)) for p in skipped],
    }
    _emit(record)


def _emit(record: dict[str, typing.Any]) -> None:
    pyfltr.output.jsonl_emit.emit_record(pyfltr.output.jsonl_records.dump_record(record))
