"""grepのマッチ本文プレビュー生成。

`MatchRecord`は走査結果の本文を全文で保持し、検索・件数上限・集計はその全文を用いる。
本モジュールは出力直前に本文だけを有界化する変換を担い、
CLIのtext / json / JSONLとMCPの各経路が同じ上限と同じ切り詰め表現を共有する。
"""

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
    """1マッチ分の有界化した本文。

    Attributes:
        line_text: マッチ行の本文プレビュー
        match_text: マッチ文字列のプレビュー
        before_lines: `-B`コンテキストの前行プレビュー
        after_lines: `-A`コンテキストの後行プレビュー
        line_text_offset: `line_text`を切り出した開始位置（0-origin文字数、行頭からなら0）
        truncated_fields: 切り詰めが発生したフィールド名（`line_text`・`match_text`・`before`・`after`）
    """

    line_text: str
    match_text: str
    before_lines: list[str]
    after_lines: list[str]
    line_text_offset: int
    truncated_fields: tuple[str, ...]

    @property
    def truncated(self) -> bool:
        """いずれかのフィールドで切り詰めが発生したかを返す。警告の発行条件に使う。"""
        return bool(self.truncated_fields)

    @property
    def line_text_truncated(self) -> bool:
        """`line_text`が切り詰められたかを返す。

        text形式が切り出し開始位置を表示するかの判定に使う。
        `match_text`や前後コンテキストだけが切り詰められた場合は`line_text`が元のままであり、
        表示すべき切り出し開始位置が無いため`truncated`と区別する。
        """
        return "line_text" in self.truncated_fields


def build_match_preview(record: MatchRecord, *, max_chars: int) -> MatchPreview:
    """`MatchRecord`から有界化した本文を組み立てる。

    `max_chars`が0以下のときは`record`の本文をそのまま返し、`truncated_fields`は空になる。
    `line_text`は、マッチ開始列が行本文の文字を指す場合、その位置を含む窓で切り出す。行頭から一律に切り詰めると、
    巨大な単一行の末尾付近に一致したマッチが表示範囲から外れ、
    一致箇所を返すというgrepの目的自体を達成できないためである。
    マルチライン一致が改行から始まる場合、`record.col`は改行を除く`line_text`の行末直後を指し、
    `_window_around`は行末側へ配置した窓を返す。実際の一致内容は`match_text`が保持する。
    窓の開始位置は`line_text_offset`が示し、`record.col`は元の行における位置のまま変えない。
    `match_text`と前後コンテキストの各行は先頭から`max_chars`文字までを返す。
    """
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
    """開始列を基準にした`max_chars`文字の窓を切り出し、本文と開始位置を返す。

    `col`は1-originの開始位置を表す。行本文の文字を指す場合は窓の中央に配置し、
    改行を指して行末直後になる場合を含め、行頭・行末では行の範囲内へ収める。
    """
    if len(text) <= max_chars:
        return text, 0
    start = max(0, (col - 1) - max_chars // 2)
    start = min(start, len(text) - max_chars)
    return text[start : start + max_chars], start


def build_truncation_warning(*, truncated_matches: int, max_chars: int, full_text_hint: str) -> str:
    """切り詰めが発生した実行で1件だけ発行する警告メッセージを組み立てる。

    `full_text_hint`は全文を取得する指定方法を経路ごとに受け取る
    （CLIは`--max-preview-chars=0`、MCPは`max_preview_chars=0`）。
    """
    return (
        f"{truncated_matches}件のマッチで本文を{max_chars}文字までのプレビューへ切り詰めました。"
        f"全文を取得するには{full_text_hint}を指定してください。"
    )
