"""置換適用ロジック。

ファイルを1単位として読み込み・置換適用・結果返却までを担う。
ファイル書き込み判定（dry-run判定）はCLI層の責務とし、本モジュールは
「置換後内容と各置換箇所のレコードを返却する」ところまでに閉じる。
"""

import hashlib
import pathlib
import re

import pyfltr.grep_.scanner
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
        encoding: ファイル読み込み時のエンコーディング（書き込みは呼び出し側の責務）

    Returns:
        `(before_content, after_content, count, records)`の4要素タプル。
        `count`は実際に置換された箇所数、`records`は各置換箇所のレコード。

    Note:
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


def apply_block_replace_to_file(
    file: pathlib.Path,
    search_pattern: re.Pattern[str],
    replacement: str,
    anchor: re.Pattern[str],
    *,
    before_context: int,
    after_context: int,
    encoding: str,
) -> tuple[str, str, int, list[ReplaceRecord]]:
    r"""アンカーで定めた行範囲集合へ限定して単一ファイルへ置換を適用する。

    `replace --within`のブロック内限定置換の本体。アンカーにマッチした行の前後
    コンテキストで定まる領域（`compute_block_ranges`）の内側に完全包含される
    検索マッチだけを置換する。

    領域を切り出してから`subn`するのではなく、ファイル全文に対して`finditer`し、
    マッチ範囲が許可文字範囲へ完全包含されるもののみ採用してオフセットベースで
    再構成する。これにより`^`/`$`/`\\A`/`\\Z`/前後読みの評価対象がファイル全体置換
    （`apply_replace_to_file`）と一致し、領域切り出しによる挙動差が生じない。

    Args:
        file: 対象ファイル
        search_pattern: 領域内で置換する検索パターン（`compile_pattern()`生成済み）
        replacement: `re.sub`互換の置換式（`\\1`/`\\g<name>`参照可）
        anchor: 領域の起点を決めるアンカーパターン（`compile_pattern()`生成済み）
        before_context: アンカー行の前に含める行数（`-B`、0以上）
        after_context: アンカー行の後に含める行数（`-A`、0以上）
        encoding: ファイル読み込み時のエンコーディング（書き込みは呼び出し側の責務）

    Returns:
        `(before_content, after_content, count, records)`の4要素タプル。
        `count`は領域内で実置換した件数で、領域外のマッチは含めない。
    """
    before_content = file.read_text(encoding=encoding)
    line_ranges = pyfltr.grep_.scanner.compute_block_ranges(
        before_content,
        anchor,
        before_context=before_context,
        after_context=after_context,
    )
    char_ranges = _line_ranges_to_char_ranges(before_content, line_ranges)

    pieces: list[str] = []
    cursor = 0
    count = 0
    for m in search_pattern.finditer(before_content):
        if not _offset_in_ranges(m.start(), m.end(), char_ranges):
            continue
        pieces.append(before_content[cursor : m.start()])
        pieces.append(m.expand(replacement))
        cursor = m.end()
        count += 1
    pieces.append(before_content[cursor:])
    after_content = "".join(pieces)

    records: list[ReplaceRecord] = []
    if count > 0:
        records = _build_replace_records(
            file=file,
            pattern=search_pattern,
            replacement=replacement,
            before_content=before_content,
            char_ranges=char_ranges,
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
    char_ranges: list[tuple[int, int]] | None = None,
) -> list[ReplaceRecord]:
    """各置換箇所の`ReplaceRecord`を組み立てる。

    `pattern.finditer(before_content)`でマッチ位置を再走査し、
    `Match.expand(replacement)`で実際に挿入される文字列を取り出す。
    `before_line`/`after_line`は当該マッチを含む論理行の置換前後本文（改行を除く）を格納する。

    `after_line`は当該マッチ箇所のみを置換した行（他のマッチによる影響を受けない）を表現するため、
    1マッチごとに`Match.string[start:end]`部分を`replacement`で差し替えた行テキストで構築する。

    `char_ranges`を渡すと、ブロック内限定置換（`apply_block_replace_to_file`）と同じく
    許可文字範囲へ完全包含されるマッチだけをレコード化する。`None`なら全マッチを対象とする。
    """
    line_starts = _line_start_offsets(before_content)
    lines_before = before_content.splitlines()
    records: list[ReplaceRecord] = []
    for m in pattern.finditer(before_content):
        if char_ranges is not None and not _offset_in_ranges(m.start(), m.end(), char_ranges):
            continue
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


def _line_ranges_to_char_ranges(text: str, line_ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """0-origin半開区間の行範囲集合を文字オフセットの半開区間へ変換する。

    `_line_start_offsets`が返す`line_starts`の要素数は、末尾改行ありで論理行数+1、
    末尾改行なしで論理行数と一致する。このため`end_line == len(lines)`のとき、
    末尾改行ありなら添字が有効だが、末尾改行なしでは`line_starts`の範囲を超える。
    範囲外の場合は`len(text)`へクランプして領域終端をファイル末尾に揃える。
    """
    line_starts = _line_start_offsets(text)
    result: list[tuple[int, int]] = []
    for start_line, end_line in line_ranges:
        char_start = line_starts[start_line] if start_line < len(line_starts) else len(text)
        char_end = line_starts[end_line] if end_line < len(line_starts) else len(text)
        result.append((char_start, char_end))
    return result


def _offset_in_ranges(start: int, end: int, char_ranges: list[tuple[int, int]]) -> bool:
    """マッチ文字範囲`[start, end)`がいずれかの許可文字範囲へ完全包含されるか判定する。"""
    return any(range_start <= start and end <= range_end for range_start, range_end in char_ranges)
