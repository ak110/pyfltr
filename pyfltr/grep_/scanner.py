import codecs
import collections.abc
import pathlib
import re

import pyfltr.paths
import pyfltr.warnings_
from pyfltr.grep_.types import FileMatchSummary, MatchRecord

_TYPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "python": ("*.py", "*.pyi"),
    "rust": ("*.rs",),
    "ts": ("*.ts", "*.tsx"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "md": ("*.md", "*.markdown"),
    "json": ("*.json",),
    "toml": ("*.toml",),
    "yaml": ("*.yaml", "*.yml"),
    "shell": ("*.sh", "*.bash", "*.zsh"),
}


def emit_read_failure_warning(source: str, file: pathlib.Path, exc: Exception, *, encoding: str) -> None:
    path = pyfltr.paths.normalize_separators(file)
    if isinstance(exc, UnicodeDecodeError):
        pyfltr.warnings_.emit_warning(
            source=source,
            message=f"文字コード {encoding} で読めないためスキップしました: {path}",
            hint="ファイルの文字コードを `--encoding`（MCPでは`encoding`）で指定してください。",
        )
        return
    pyfltr.warnings_.emit_warning(
        source=source,
        message=f"ファイルを読み込めないためスキップしました: {path}: {getattr(exc, 'strerror', None) or exc}",
        hint="ファイルの存在と読み取り権限を確認してください。",
    )


def filter_files_by_type(files: list[pathlib.Path], types: list[str]) -> list[pathlib.Path]:
    if not types:
        return list(files)
    patterns: list[str] = []
    for type_name in types:
        patterns.extend(_TYPE_PATTERNS.get(type_name, ()))
    if not patterns:
        return []
    return [f for f in files if any(f.match(p) for p in patterns)]


def filter_by_globs(files: list[pathlib.Path], globs: list[str]) -> list[pathlib.Path]:
    if not globs:
        return list(files)
    return [f for f in files if any(f.match(g) for g in globs)]


def scan_files(
    files: list[pathlib.Path],
    pattern: re.Pattern[str],
    *,
    before_context: int,
    after_context: int,
    max_per_file: int,
    max_total: int,
    encoding: str,
    max_filesize: int | None,
    multiline: bool,
) -> collections.abc.Iterator[MatchRecord | FileMatchSummary]:
    total = 0
    for file in files:
        if 0 < max_total <= total:
            return
        if max_filesize is not None and max_filesize > 0:
            try:
                if file.stat().st_size > max_filesize:
                    continue
            except OSError:
                continue
        try:
            text = _read_search_text(file, encoding)
        except (UnicodeDecodeError, OSError) as exc:
            emit_read_failure_warning("grep", file, exc, encoding=encoding)
            continue
        for per_file, record in enumerate(
            _scan_text(
                file=file,
                text=text,
                pattern=pattern,
                before_context=before_context,
                after_context=after_context,
                multiline=multiline,
            )
        ):
            if 0 < max_per_file <= per_file:
                break
            yield record
            total += 1
            if 0 < max_total <= total:
                return


def _read_search_text(file: pathlib.Path, encoding: str) -> str:
    try:
        return file.read_text(encoding=encoding)
    except UnicodeDecodeError:
        if codecs.lookup(encoding).name != "utf-8":
            raise
    return file.read_text(encoding="cp932")


def _scan_text(
    *,
    file: pathlib.Path,
    text: str,
    pattern: re.Pattern[str],
    before_context: int,
    after_context: int,
    multiline: bool,
) -> collections.abc.Iterator[MatchRecord]:
    lines = text.splitlines()
    if not lines:
        return
    if multiline:
        yield from _scan_multiline(
            file=file,
            text=text,
            lines=lines,
            pattern=pattern,
            before_context=before_context,
            after_context=after_context,
        )
        return
    last_after_end_line = 0
    for line_index, line_text in enumerate(lines):
        line_no = line_index + 1
        for m in pattern.finditer(line_text):
            start_col = m.start() + 1
            end_col = m.end() + 1
            before_start = max(0, line_index - before_context)
            before_start = max(before_start, last_after_end_line)
            before_lines = lines[before_start:line_index]
            after_end = min(len(lines), line_index + 1 + after_context)
            after_lines = lines[line_index + 1 : after_end]
            yield MatchRecord(
                file=file,
                line=line_no,
                col=start_col,
                end_col=end_col,
                line_text=line_text,
                match_text=m.group(0),
                before_lines=list(before_lines),
                after_lines=list(after_lines),
            )
            last_after_end_line = after_end


def _scan_multiline(
    *,
    file: pathlib.Path,
    text: str,
    lines: list[str],
    pattern: re.Pattern[str],
    before_context: int,
    after_context: int,
) -> collections.abc.Iterator[MatchRecord]:
    line_starts: list[int] = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)
    last_after_end_line = 0
    for m in pattern.finditer(text):
        start_pos = m.start()
        end_pos = m.end()
        line_index = _find_line_index(line_starts, start_pos)
        end_line_index = _find_line_index(line_starts, max(end_pos - 1, start_pos))
        line_no = line_index + 1
        col = start_pos - line_starts[line_index] + 1
        end_col = end_pos - line_starts[end_line_index] + 1
        line_text = lines[line_index] if line_index < len(lines) else ""
        match_text = m.group(0)
        before_start = max(0, line_index - before_context)
        before_start = max(before_start, last_after_end_line)
        before_lines = lines[before_start:line_index]
        after_end = min(len(lines), end_line_index + 1 + after_context)
        after_lines = lines[end_line_index + 1 : after_end]
        yield MatchRecord(
            file=file,
            line=line_no,
            col=col,
            end_col=end_col,
            line_text=line_text,
            match_text=match_text,
            before_lines=list(before_lines),
            after_lines=list(after_lines),
        )
        last_after_end_line = after_end


def _find_line_index(line_starts: list[int], pos: int) -> int:
    line_index = 0
    for i, start in enumerate(line_starts):
        if start <= pos:
            line_index = i
        else:
            break
    return line_index


def compute_block_ranges(
    text: str,
    anchor: re.Pattern[str],
    *,
    before_context: int,
    after_context: int,
) -> list[tuple[int, int]]:
    lines = text.splitlines()
    ranges: list[tuple[int, int]] = []
    for line_index, line_text in enumerate(lines):
        if anchor.search(line_text):
            start = max(0, line_index - before_context)
            end = min(len(lines), line_index + 1 + after_context)
            ranges.append((start, end))
    return _merge_line_ranges(ranges)


def _merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged: list[tuple[int, int]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged
