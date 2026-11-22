from __future__ import annotations

import dataclasses

from pyfltr.grep_.types import MatchRecord

DEFAULT_MAX_PREVIEW_CHARS = 200
"""本文フィールド1件あたりのプレビュー上限（文字数）。

対象リポジトリの追跡ファイル66148行の実測では、200文字を超える行は1.722%であり、
通常のソース行（p50=33、p90=76、p95=91）と文書行は全文のまま返る。
80文字では8.126%が切り詰められて日常的な検索の可読性を損ない、
320文字（超過0.656%）は200文字との間に該当する行がほとんど無く応答量だけが増える。
240文字は超過率が1.720%で200文字とほぼ同じため、小さい側を採用する。
CLIとMCPで同じ値を用いる（`.claude/skills/grep-replace/SKILL.md`の「本文プレビューの上限」を参照）。
"""


@dataclasses.dataclass(frozen=True)
class MatchPreview:
    line_text: str
    match_text: str
    before_lines: list[str]
    after_lines: list[str]
    line_text_offset: int
    truncated_fields: tuple[str, ...]

    @property
    def truncated(self) -> bool:
        return bool(self.truncated_fields)

    @property
    def line_text_truncated(self) -> bool:
        return "line_text" in self.truncated_fields


def build_match_preview(record: MatchRecord, *, max_chars: int) -> MatchPreview:
    if max_chars <= 0:
        return MatchPreview(
            line_text=record.line_text,
            match_text=record.match_text,
            before_lines=list(record.before_lines),
            after_lines=list(record.after_lines),
            line_text_offset=0,
            truncated_fields=(),
        )
    truncated: list[str] = []
    line_text, offset = _window_around(record.line_text, col=record.col, max_chars=max_chars)
    if line_text != record.line_text:
        truncated.append("line_text")
    match_text = record.match_text[:max_chars]
    if match_text != record.match_text:
        truncated.append("match_text")
    before_lines = [line[:max_chars] for line in record.before_lines]
    if before_lines != list(record.before_lines):
        truncated.append("before")
    after_lines = [line[:max_chars] for line in record.after_lines]
    if after_lines != list(record.after_lines):
        truncated.append("after")
    return MatchPreview(
        line_text=line_text,
        match_text=match_text,
        before_lines=before_lines,
        after_lines=after_lines,
        line_text_offset=offset,
        truncated_fields=tuple(truncated),
    )


def _window_around(text: str, *, col: int, max_chars: int) -> tuple[str, int]:
    if len(text) <= max_chars:
        return text, 0
    start = max(0, (col - 1) - max_chars // 2)
    start = min(start, len(text) - max_chars)
    return text[start : start + max_chars], start


def build_truncation_warning(*, truncated_matches: int, max_chars: int, full_text_hint: str) -> str:
    return (
        f"{truncated_matches}件のマッチで本文を{max_chars}文字までのプレビューへ切り詰めました。"
        f"全文を取得するには{full_text_hint}を指定してください。"
    )
