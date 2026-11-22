from __future__ import annotations

import pathlib
import typing

import pyfltr.output.logging_
import pyfltr.paths
import pyfltr.warnings_
from pyfltr.grep_.preview import MatchPreview
from pyfltr.grep_.types import MatchRecord, ReplaceRecord


def render_match(record: MatchRecord, preview: MatchPreview) -> None:
    path = pyfltr.paths.normalize_separators(str(record.file))
    preview_prefix = f"[+{preview.line_text_offset}] " if preview.line_text_truncated else ""
    line = f"{path}:{record.line}:{record.col}:{preview_prefix}{preview.line_text}"
    with pyfltr.output.logging_.text_output_lock:
        pyfltr.output.logging_.text_logger.info(line)


def render_filtered_sections(
    *,
    warnings: list[dict[str, typing.Any]],
    missing_targets: list[str],
    fully_excluded_files: list[str],
) -> None:
    with pyfltr.output.logging_.text_output_lock:
        if warnings:
            pyfltr.output.logging_.text_logger.info(f"{'-' * 10} warnings {'-' * (72 - 10 - 10)}")
            for entry in warnings:
                pyfltr.output.logging_.text_logger.info(
                    f"    [{entry['source']}] {pyfltr.warnings_.format_warning_text(entry)}"
                )
        if missing_targets:
            pyfltr.output.logging_.text_logger.info(f"{'-' * 10} missing-targets {'-' * (72 - 10 - 17)}")
            for path in missing_targets:
                pyfltr.output.logging_.text_logger.info(f"    {path}")
        if fully_excluded_files:
            pyfltr.output.logging_.text_logger.info(f"{'-' * 10} fully-excluded-files {'-' * (72 - 10 - 22)}")
            for path in fully_excluded_files:
                pyfltr.output.logging_.text_logger.info(f"    {path}")


def render_grep_summary(
    *,
    total_matches: int,
    files_with_matches: int,
    files_scanned: int,
) -> None:
    summary = f"-- {total_matches} match(es) in {files_with_matches} file(s) (scanned {files_scanned} file(s))"
    with pyfltr.output.logging_.text_output_lock:
        pyfltr.output.logging_.text_logger.info(summary)


def render_grep_guidance(commands: list[str]) -> None:
    if not commands:
        return
    with pyfltr.output.logging_.text_output_lock:
        for line in commands:
            pyfltr.output.logging_.text_logger.info(line)


def render_file_change(
    *,
    file: pathlib.Path,
    count: int,
    dry_run: bool,
) -> None:
    prefix = "(dry-run) " if dry_run else ""
    path = pyfltr.paths.normalize_separators(str(file))
    line = f"{prefix}M {path} ({count} replacement(s))"
    with pyfltr.output.logging_.text_output_lock:
        pyfltr.output.logging_.text_logger.info(line)


def render_change_diff(record: ReplaceRecord) -> None:
    with pyfltr.output.logging_.text_output_lock:
        pyfltr.output.logging_.text_logger.info(f"- {record.before_line}")
        pyfltr.output.logging_.text_logger.info(f"+ {record.after_line}")


def render_replace_summary(
    *,
    files_changed: int,
    total_replacements: int,
    dry_run: bool,
    replace_id: str | None,
) -> None:
    prefix = "(dry-run) " if dry_run else ""
    suffix = f" replace_id={replace_id}" if replace_id else ""
    line = f"-- {prefix}{total_replacements} replacement(s) in {files_changed} file(s){suffix}"
    with pyfltr.output.logging_.text_output_lock:
        pyfltr.output.logging_.text_logger.info(line)


def render_replace_guidance(commands: list[str]) -> None:
    if not commands:
        return
    with pyfltr.output.logging_.text_output_lock:
        for line in commands:
            pyfltr.output.logging_.text_logger.info(line)


def render_undo_summary(
    *,
    replace_id: str,
    restored: list[pathlib.Path],
    skipped: list[pathlib.Path],
) -> None:
    with pyfltr.output.logging_.text_output_lock:
        pyfltr.output.logging_.text_logger.info(
            f"-- undo replace_id={replace_id}: restored={len(restored)} skipped={len(skipped)}"
        )
        for path in restored:
            pyfltr.output.logging_.text_logger.info(f"  restored: {pyfltr.paths.normalize_separators(str(path))}")
        for path in skipped:
            pyfltr.output.logging_.text_logger.info(f"  skipped: {pyfltr.paths.normalize_separators(str(path))}")
