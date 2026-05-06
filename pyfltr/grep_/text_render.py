"""grep / replaceのtext形式人間向け出力。

色付けは省略し、ripgrep流儀の`path:line:col:line_text`形式で1行ずつ出力する。
出力先は`pyfltr.cli.output_format.text_logger`（INFO以上をstdoutへ）に揃え、
行間の混入を避けるため`text_output_lock`で保護する。
"""

from __future__ import annotations

import pathlib

import pyfltr.cli.output_format
import pyfltr.paths
from pyfltr.grep_.types import MatchRecord, ReplaceRecord


def render_match(record: MatchRecord) -> None:
    """grepの1マッチを`path:line:col:line_text`形式で1行出力する。

    `before`・`after`コンテキストは省略する（テキスト出力ではマッチ本体のみで十分）。
    """
    path = pyfltr.paths.normalize_separators(str(record.file))
    line = f"{path}:{record.line}:{record.col}:{record.line_text}"
    with pyfltr.cli.output_format.text_output_lock:
        pyfltr.cli.output_format.text_logger.info(line)


def render_grep_summary(
    *,
    total_matches: int,
    files_with_matches: int,
    files_scanned: int,
) -> None:
    """grep完了時の集計行を出力する。"""
    summary = f"-- {total_matches} match(es) in {files_with_matches} file(s) (scanned {files_scanned} file(s))"
    with pyfltr.cli.output_format.text_output_lock:
        pyfltr.cli.output_format.text_logger.info(summary)


def render_grep_guidance(commands: list[str]) -> None:
    """grep完了時のガイダンス行（replace起動コマンド案内など）を出力する。"""
    if not commands:
        return
    with pyfltr.cli.output_format.text_output_lock:
        for line in commands:
            pyfltr.cli.output_format.text_logger.info(line)


def render_file_change(
    *,
    file: pathlib.Path,
    count: int,
    dry_run: bool,
) -> None:
    """1ファイル分の置換結果サマリ行を出力する。"""
    prefix = "(dry-run) " if dry_run else ""
    path = pyfltr.paths.normalize_separators(str(file))
    line = f"{prefix}M {path} ({count} replacement(s))"
    with pyfltr.cli.output_format.text_output_lock:
        pyfltr.cli.output_format.text_logger.info(line)


def render_change_diff(record: ReplaceRecord) -> None:
    """`--show-changes`用の差分行を出力する。

    `- {before_line}` / `+ {after_line}` の2行で構成し、利用者がbefore / afterを並列に確認できる。
    """
    with pyfltr.cli.output_format.text_output_lock:
        pyfltr.cli.output_format.text_logger.info(f"- {record.before_line}")
        pyfltr.cli.output_format.text_logger.info(f"+ {record.after_line}")


def render_replace_summary(
    *,
    files_changed: int,
    total_replacements: int,
    dry_run: bool,
    replace_id: str | None,
) -> None:
    """replace完了時の集計行を出力する。"""
    prefix = "(dry-run) " if dry_run else ""
    suffix = f" replace_id={replace_id}" if replace_id else ""
    line = f"-- {prefix}{total_replacements} replacement(s) in {files_changed} file(s){suffix}"
    with pyfltr.cli.output_format.text_output_lock:
        pyfltr.cli.output_format.text_logger.info(line)


def render_replace_guidance(commands: list[str]) -> None:
    """replace完了時のガイダンス行（undoコマンド案内など）を出力する。"""
    if not commands:
        return
    with pyfltr.cli.output_format.text_output_lock:
        for line in commands:
            pyfltr.cli.output_format.text_logger.info(line)


def render_undo_summary(
    *,
    replace_id: str,
    restored: list[pathlib.Path],
    skipped: list[pathlib.Path],
) -> None:
    """undo完了時の集計行を出力する。"""
    with pyfltr.cli.output_format.text_output_lock:
        pyfltr.cli.output_format.text_logger.info(
            f"-- undo replace_id={replace_id}: restored={len(restored)} skipped={len(skipped)}"
        )
        for path in restored:
            pyfltr.cli.output_format.text_logger.info(f"  restored: {pyfltr.paths.normalize_separators(str(path))}")
        for path in skipped:
            pyfltr.cli.output_format.text_logger.info(f"  skipped: {pyfltr.paths.normalize_separators(str(path))}")
