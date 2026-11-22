import codecs
import dataclasses
import hashlib
import pathlib
import re

import pyfltr.grep_.scanner
from pyfltr.grep_.types import ReplaceRecord


@dataclasses.dataclass(frozen=True)
class ReplacementResult:
    before_content: str
    after_content: str
    before_bytes: bytes
    after_bytes: bytes
    count: int
    records: list[ReplaceRecord]


@dataclasses.dataclass(frozen=True)
class _ReplacementInput:
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
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _build_replace_records(
    *,
    file: pathlib.Path,
    pattern: re.Pattern[str],
    replacement: str,
    before_content: str,
    char_ranges: list[tuple[int, int]] | None = None,
) -> list[ReplaceRecord]:
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
    offsets = [0]
    position = 0
    for line in text.splitlines(keepends=True):
        position += len(line)
        if line[-1] in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029":
            offsets.append(position)
    return offsets


def _line_of(line_starts: list[int], pos: int) -> int:
    line_index = 0
    for i, start in enumerate(line_starts):
        if start <= pos:
            line_index = i
        else:
            break
    return line_index


def _line_ranges_to_char_ranges(text: str, line_ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    line_starts = _line_start_offsets(text)
    result: list[tuple[int, int]] = []
    for start_line, end_line in line_ranges:
        char_start = line_starts[start_line] if start_line < len(line_starts) else len(text)
        char_end = line_starts[end_line] if end_line < len(line_starts) else len(text)
        result.append((char_start, char_end))
    return result


def _offset_in_ranges(start: int, end: int, char_ranges: list[tuple[int, int]]) -> bool:
    return any(range_start <= start and end <= range_end for range_start, range_end in char_ranges)
