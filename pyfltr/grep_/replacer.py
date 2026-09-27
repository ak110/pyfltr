"""置換適用ロジック。

ファイルを1単位として読み込み・置換適用・結果返却までを担う。
ファイル書き込み判定（dry-run判定）はCLI層の責務とし、本モジュールは
「置換後内容と各置換箇所のレコードを返却する」ところまでに閉じる。
"""

import codecs
import dataclasses
import hashlib
import pathlib
import re

import pyfltr.grep_.scanner
from pyfltr.grep_.types import ReplaceRecord


@dataclasses.dataclass(frozen=True)
class ReplacementResult:
    """検索・公開ハッシュ用の文字列と復元・書き込み用バイト列を分けて保持する。"""

    before_content: str
    after_content: str
    before_bytes: bytes
    after_bytes: bytes
    count: int
    records: list[ReplaceRecord]


@dataclasses.dataclass(frozen=True)
class _ReplacementInput:
    """デコード後の文字位置と元バイト位置の対応を保持する。"""

    original: bytes
    body: bytes
    text: str
    byte_offsets: list[int]
    encoding: str
    bom: bytes


def apply_replace_to_file(
    file: pathlib.Path,
    pattern: re.Pattern[str],
    replacement: str,
    *,
    encoding: str,
) -> ReplacementResult:
    r"""単一ファイルへ置換を適用する。

    Args:
        file: 対象ファイル
        pattern: `compile_pattern()`で生成済みの`re.Pattern`。
            マルチライン要否のフラグ（`re.DOTALL | re.MULTILINE`）は
            `compile_pattern`側で組み込まれており、本関数では追加の指定を取らない
        replacement: `re.sub`互換の置換式（`\\1`/`\\g<name>`参照可）
        encoding: ファイルの読み込みと置換結果の符号化に使うエンコーディング

    Returns:
        置換前後の文字列・バイト列、置換件数及び各置換箇所のレコード。

    Note:
        マッチが行を跨ぐ場合（マルチラインモード）は、開始行を基準にした`ReplaceRecord`を生成し
        `before_line`に「マッチ開始行の置換前テキスト」、`after_line`に「マッチ開始行の置換後テキスト」を
        格納する。
    """
    source = _read_content(file, encoding)
    before_content, raw_offsets = _search_view(source.text)
    after_content, after_bytes, count = _replace_matches(before_content, source, raw_offsets, pattern, replacement)
    records: list[ReplaceRecord] = []
    if count > 0:
        records = _build_replace_records(
            file=file,
            pattern=pattern,
            replacement=replacement,
            before_content=before_content,
        )
    return ReplacementResult(before_content, after_content, source.original, source.bom + after_bytes, count, records)


def apply_block_replace_to_file(
    file: pathlib.Path,
    search_pattern: re.Pattern[str],
    replacement: str,
    anchor: re.Pattern[str],
    *,
    before_context: int,
    after_context: int,
    encoding: str,
) -> ReplacementResult:
    r"""アンカーで定めた行範囲集合へ限定して単一ファイルへ置換を適用する。

    `replace --within`のブロック内限定置換の本体。アンカーにマッチした行の前後
    コンテキストで定まる領域（`compute_block_ranges`）の内側に完全包含される
    検索マッチだけを置換する。

    領域を切り出してから`subn`するのではなく、通常置換と同じ検索用ビューから
    マッチ範囲が許可文字範囲へ完全包含されるものだけを採用する。
    このため`^`/`$`/`\\A`/`\\Z`/前後読みの評価対象は通常置換と一致する。

    Args:
        file: 対象ファイル
        search_pattern: 領域内で置換する検索パターン（`compile_pattern()`生成済み）
        replacement: `re.sub`互換の置換式（`\\1`/`\\g<name>`参照可）
        anchor: 領域の起点を決めるアンカーパターン（`compile_pattern()`生成済み）
        before_context: アンカー行の前に含める行数（`-B`、0以上）
        after_context: アンカー行の後に含める行数（`-A`、0以上）
        encoding: ファイルの読み込みと置換結果の符号化に使うエンコーディング

    Returns:
        置換前後の文字列・バイト列、領域内の置換件数及び各置換箇所のレコード。
    """
    source = _read_content(file, encoding)
    before_content, raw_offsets = _search_view(source.text)
    line_ranges = pyfltr.grep_.scanner.compute_block_ranges(
        before_content,
        anchor,
        before_context=before_context,
        after_context=after_context,
    )
    char_ranges = _line_ranges_to_char_ranges(before_content, line_ranges)

    after_content, after_bytes, count = _replace_matches(
        before_content, source, raw_offsets, search_pattern, replacement, char_ranges=char_ranges
    )

    records: list[ReplaceRecord] = []
    if count > 0:
        records = _build_replace_records(
            file=file,
            pattern=search_pattern,
            replacement=replacement,
            before_content=before_content,
            char_ranges=char_ranges,
        )
    return ReplacementResult(before_content, after_content, source.original, source.bom + after_bytes, count, records)


def _read_content(file: pathlib.Path, encoding: str) -> _ReplacementInput:
    """BOMと元のバイト位置を保持して対象をデコードする。"""
    content = file.read_bytes()
    codec = codecs.lookup(encoding).name
    bom = b""
    if codec == "utf-16":
        if content.startswith(codecs.BOM_UTF16_LE):
            bom = codecs.BOM_UTF16_LE
            codec = "utf-16-le"
        elif content.startswith(codecs.BOM_UTF16_BE):
            bom = codecs.BOM_UTF16_BE
            codec = "utf-16-be"
        else:
            content.decode(encoding)
    elif codec == "utf-8-sig":
        bom = codecs.BOM_UTF8 if content.startswith(codecs.BOM_UTF8) else b""
        codec = "utf-8"
    body = content[len(bom) :]
    decoder = codecs.getincrementaldecoder(codec)(errors="strict")
    characters: list[str] = []
    byte_offsets = [0]
    for index, byte in enumerate(body, start=1):
        decoded = decoder.decode(bytes((byte,)))
        if decoded:
            characters.append(decoded)
            byte_offsets.extend([index] * len(decoded))
    final = decoder.decode(b"", final=True)
    if final:
        characters.append(final)
        byte_offsets.extend([len(body)] * len(final))
    return _ReplacementInput(content, body, "".join(characters), byte_offsets, codec, bom)


def _search_view(content: str) -> tuple[str, list[int]]:
    """従来と同じ改行変換後の検索文字列と、元文字列への境界位置を返す。"""
    search_chars: list[str] = []
    raw_offsets = [0]
    index = 0
    while index < len(content):
        if content[index] == "\r":
            index += 2 if content[index : index + 2] == "\r\n" else 1
            search_chars.append("\n")
        else:
            search_chars.append(content[index])
            index += 1
        raw_offsets.append(index)
    return "".join(search_chars), raw_offsets


def _replace_matches(
    content: str,
    source: _ReplacementInput,
    raw_offsets: list[int],
    pattern: re.Pattern[str],
    replacement: str,
    *,
    char_ranges: list[tuple[int, int]] | None = None,
) -> tuple[str, bytes, int]:
    """従来の検索文字列で照合し、非置換部分は元バイト列からつなぐ。"""
    content_parts: list[str] = []
    byte_parts: list[bytes] = []
    cursor = 0
    count = 0
    for match in pattern.finditer(content):
        if char_ranges is not None and not _offset_in_ranges(match.start(), match.end(), char_ranges):
            continue
        expanded = match.expand(replacement)
        content_parts.extend((content[cursor : match.start()], expanded))
        byte_parts.extend(
            (
                source.body[source.byte_offsets[raw_offsets[cursor]] : source.byte_offsets[raw_offsets[match.start()]]],
                expanded.encode(source.encoding),
            )
        )
        cursor = match.end()
        count += 1
    content_parts.append(content[cursor:])
    byte_parts.append(source.body[source.byte_offsets[raw_offsets[cursor]] :])
    return "".join(content_parts), b"".join(byte_parts), count


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
    position = 0
    for line in text.splitlines(keepends=True):
        position += len(line)
        if line[-1] in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            offsets.append(position)
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
