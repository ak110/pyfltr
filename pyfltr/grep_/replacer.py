"""置換適用ロジック。

ファイルを1単位として読み込み・置換適用・結果返却までを担う。
ファイル書き込み判定（dry-run判定）はCLI層の責務とし、本モジュールは
「置換後内容と各置換箇所のレコードを返却する」ところまでに閉じる。
"""

import hashlib
import pathlib
import re

from pyfltr.grep_.types import ReplaceRecord


def apply_replace_to_file(
    file: pathlib.Path,
    pattern: re.Pattern[str],
    replacement: str,
    *,
    encoding: str,
) -> tuple[str, str, int, list[ReplaceRecord]]:
    r"""単一ファイルへ置換を適用する。

    Args:
        file: 対象ファイル
        pattern: `compile_pattern()`で生成済みの`re.Pattern`。
            マルチライン要否のフラグ（`re.DOTALL | re.MULTILINE`）は
            `compile_pattern`側で組み込まれており、本関数では追加の指定を取らない
        replacement: `re.sub`互換の置換式（`\\1`/`\\g<name>`参照可）
        encoding: ファイル読み込み・書き込み時のエンコーディング

    Returns:
        `(before_content, after_content, count, records)`の4要素タプル。
        `count`は実際に置換された箇所数、`records`は各置換箇所のレコード。

    マッチが行を跨ぐ場合（マルチラインモード）は、開始行を基準にした`ReplaceRecord`を生成し
    `before_line`に「マッチ開始行の置換前テキスト」、`after_line`に「マッチ開始行の置換後テキスト」を
    格納する。
    """
    before_content = file.read_text(encoding=encoding)
    after_content, count = pattern.subn(replacement, before_content)
    records: list[ReplaceRecord] = []
    if count > 0:
        records = _build_replace_records(
            file=file,
            pattern=pattern,
            replacement=replacement,
            before_content=before_content,
        )
    return before_content, after_content, count, records


def compute_hash(content: str) -> str:
    """内容のSHA-256ハッシュ16進文字列を返す。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_replace_records(
    *,
    file: pathlib.Path,
    pattern: re.Pattern[str],
    replacement: str,
    before_content: str,
) -> list[ReplaceRecord]:
    """各置換箇所の`ReplaceRecord`を組み立てる。

    `pattern.finditer(before_content)`でマッチ位置を再走査し、
    `Match.expand(replacement)`で実際に挿入される文字列を取り出す。
    `before_line`/`after_line`は当該マッチを含む論理行の置換前後本文（改行を除く）を格納する。

    `after_line`は当該マッチ箇所のみを置換した行（他のマッチによる影響を受けない）を表現するため、
    1マッチごとに`Match.string[start:end]`部分を`replacement`で差し替えた行テキストで構築する。
    """
    line_starts = _line_start_offsets(before_content)
    lines_before = before_content.splitlines()
    records: list[ReplaceRecord] = []
    for m in pattern.finditer(before_content):
        start_pos = m.start()
        end_pos = m.end()
        line_index = _line_of(line_starts, start_pos)
        line_no = line_index + 1
        col = start_pos - line_starts[line_index] + 1
        before_line = lines_before[line_index] if line_index < len(lines_before) else ""
        before_text = m.group(0)
        after_text = m.expand(replacement)
        # 行内置換のみを反映したafter_lineを構築する。
        # マルチラインマッチで行を跨ぐ場合は、置換前行のうち当該行に属する範囲のみ差し替える
        end_line_index = _line_of(line_starts, max(end_pos - 1, start_pos))
        if end_line_index == line_index:
            within_start = col - 1
            within_end = end_pos - line_starts[line_index]
            after_line = before_line[:within_start] + after_text + before_line[within_end:]
        else:
            within_start = col - 1
            after_line = before_line[:within_start] + after_text
        records.append(
            ReplaceRecord(
                file=file,
                line=line_no,
                col=col,
                before_line=before_line,
                after_line=after_line,
                before_text=before_text,
                after_text=after_text,
            )
        )
    return records


def _line_start_offsets(text: str) -> list[int]:
    """各論理行の開始オフセットを返す（0-origin、行0は0）。"""
    offsets = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offsets.append(i + 1)
    return offsets


def _line_of(line_starts: list[int], pos: int) -> int:
    """文字オフセットから0-origin行番号を返す。"""
    line_index = 0
    for i, start in enumerate(line_starts):
        if start <= pos:
            line_index = i
        else:
            break
    return line_index
