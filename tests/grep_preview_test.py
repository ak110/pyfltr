"""grepのマッチ本文プレビュー生成テスト。"""

import pathlib

import pytest

import pyfltr.grep_.preview
from pyfltr.grep_.types import MatchRecord


def _record(
    *,
    line_text: str,
    match_text: str = "needle",
    col: int = 1,
    before_lines: list[str] | None = None,
    after_lines: list[str] | None = None,
) -> MatchRecord:
    """本文プレビュー検体の`MatchRecord`を返す。"""
    return MatchRecord(
        file=pathlib.Path("sample.txt"),
        line=1,
        col=col,
        end_col=col + len(match_text),
        line_text=line_text,
        match_text=match_text,
        before_lines=list(before_lines or []),
        after_lines=list(after_lines or []),
    )


def test_build_match_preview_keeps_short_fields() -> None:
    record = _record(line_text="hello needle", col=7, before_lines=["before"], after_lines=["after"])

    preview = pyfltr.grep_.preview.build_match_preview(record, max_chars=20)

    assert preview.line_text == record.line_text
    assert preview.match_text == record.match_text
    assert preview.before_lines == record.before_lines
    assert preview.after_lines == record.after_lines
    assert preview.line_text_offset == 0
    assert not preview.truncated_fields


@pytest.mark.parametrize(
    ("col", "expected_offset"),
    [
        (2, 0),
        (16, 10),
        (30, 20),
    ],
)
def test_build_match_preview_windows_long_line(col: int, expected_offset: int) -> None:
    line_text = "x" * 30
    record = _record(line_text=line_text, match_text="x", col=col)

    preview = pyfltr.grep_.preview.build_match_preview(record, max_chars=10)

    assert len(preview.line_text) == 10
    assert preview.line_text_offset == expected_offset
    assert 0 <= record.col - 1 - preview.line_text_offset < len(preview.line_text)
    assert preview.truncated_fields == ("line_text",)


def test_build_match_preview_truncates_long_match() -> None:
    record = _record(line_text="x" * 20, match_text="x" * 15, col=3)

    preview = pyfltr.grep_.preview.build_match_preview(record, max_chars=10)

    assert len(preview.line_text) == 10
    assert len(preview.match_text) == 10
    assert preview.truncated_fields == ("line_text", "match_text")


def test_build_match_preview_truncates_context_lines_without_changing_count() -> None:
    record = _record(
        line_text="needle",
        before_lines=["a" * 15, "short"],
        after_lines=["b" * 15, "short"],
    )

    preview = pyfltr.grep_.preview.build_match_preview(record, max_chars=10)

    assert preview.before_lines == ["a" * 10, "short"]
    assert preview.after_lines == ["b" * 10, "short"]
    assert len(preview.before_lines) == len(record.before_lines)
    assert len(preview.after_lines) == len(record.after_lines)
    assert preview.truncated_fields == ("before", "after")
    assert not preview.line_text_truncated
    assert preview.truncated


def test_build_match_preview_zero_keeps_full_text() -> None:
    record = _record(
        line_text="x" * 30,
        match_text="x" * 20,
        col=15,
        before_lines=["a" * 30],
        after_lines=["b" * 30],
    )

    preview = pyfltr.grep_.preview.build_match_preview(record, max_chars=0)

    assert preview.line_text == record.line_text
    assert preview.match_text == record.match_text
    assert preview.before_lines == record.before_lines
    assert preview.after_lines == record.after_lines
    assert preview.line_text_offset == 0
    assert not preview.truncated_fields


def test_build_match_preview_distinguishes_match_only_truncation() -> None:
    record = _record(line_text="start", match_text="x" * 20)

    preview = pyfltr.grep_.preview.build_match_preview(record, max_chars=10)

    assert preview.line_text == record.line_text
    assert preview.line_text_offset == 0
    assert preview.truncated_fields == ("match_text",)
    assert not preview.line_text_truncated
    assert preview.truncated


def test_build_truncation_warning_includes_recovery_details() -> None:
    warning = pyfltr.grep_.preview.build_truncation_warning(
        truncated_matches=2,
        max_chars=200,
        full_text_hint="`--max-preview-chars=0`",
    )

    assert "2件" in warning
    assert "200文字" in warning
    assert "--max-preview-chars=0" in warning
