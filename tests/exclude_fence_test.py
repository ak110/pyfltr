"""見出し配下フェンス除外のテスト。"""

import pathlib

import pytest

import pyfltr.command.error_parser
import pyfltr.text.exclude_fence


def test_mask_fenced_blocks_under_heading_masks_inner_lines_only() -> None:
    """対象H2見出し配下のフェンス内側行だけを空行へ置換する。"""
    text = "## 背景\n\n```text\n長い原文です。\nsecond line\n```\n"

    masked = pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(text, ["## 背景"])

    assert masked == "## 背景\n\n```text\n\n\n```\n"
    assert masked.count("\n") == text.count("\n")


def test_mask_fenced_blocks_under_heading_keeps_other_sections() -> None:
    """指定H2見出し配下以外のフェンスは変更しない。"""
    text = "## 対象外\n\n```\nkeep\n```\n\n## 背景\n\n```\nmask\n```\n"

    masked = pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(text, ["## 背景"])

    assert "keep" in masked
    assert "mask" not in masked
    assert masked.count("\n") == text.count("\n")


def test_mask_fenced_blocks_under_heading_accepts_multiple_headings_until_eof() -> None:
    """複数H2見出しを指定でき、最終H2見出しはEOFまで対象にする。"""
    text = "## 背景\n\n~~~\nfirst\n~~~\n\n## 詳細\n\n````python\nsecond\n````\n"

    masked = pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(text, ["## 背景", "## 詳細"])

    assert "first" not in masked
    assert "second" not in masked
    assert masked.count("\n") == text.count("\n")


def test_mask_fenced_blocks_under_heading_returns_original_for_empty_or_missing_input() -> None:
    """指定が空または該当見出しが無い場合は原文を返す。"""
    text = "## 背景\n\n```\nkeep\n```\n"

    assert pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(text, []) == text
    assert pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(text, ["## 不在"]) == text
    assert pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings("", ["## 背景"]) == ""


def test_mask_fenced_blocks_under_heading_produces_empty_lines() -> None:
    """マスク後のフェンス内側行が空行（改行のみ）で長い行を残さない。"""
    long_line = "あ" * 200
    text = f"## 背景\n\n```text\n{long_line}\n```\n"

    masked = pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(text, ["## 背景"])

    assert masked == "## 背景\n\n```text\n\n```\n"
    for masked_line in masked.splitlines():
        assert len(masked_line) <= 127 or masked_line.startswith("`")


def test_mask_fenced_blocks_under_heading_uses_short_space_for_unclosed_eof() -> None:
    """未閉じフェンスかつ改行なしEOF最終行は短い空白へ置換する。"""
    text = "## 背景\n\n```text\n" + "あ" * 200

    masked = pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(text, ["## 背景"])

    assert masked.count("\n") == text.count("\n")
    assert "あ" not in masked
    assert masked.splitlines()[-1] == " "


def test_markdownlint_diagnostic_file_is_restored_from_temporary_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    """markdownlint診断の一時ファイルパスを元ファイルパスへ戻す。"""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    original_path = tmp_path / "sample.md"
    temporary_path = tmp_path / "masked" / "sample.md"
    relative_temporary_path = pathlib.Path("..", "masked", "sample.md")
    monkeypatch.chdir(work_dir)

    output = (
        f"{relative_temporary_path}:3 error MD001/heading-increment Heading levels should only increment by one level at a time"
    )
    errors = pyfltr.command.error_parser.parse_errors(
        "markdownlint",
        output,
        file_path_remap={str(temporary_path): str(original_path)},
    )

    assert len(errors) == 1
    assert errors[0].file == str(original_path)
    assert errors[0].rule == "MD001"
