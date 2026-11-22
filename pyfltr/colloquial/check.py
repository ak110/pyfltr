import pathlib
import re

import pyfltr.text.fences

_DICT_DIR = pathlib.Path(__file__).resolve().parent
DENY_PATH = _DICT_DIR / "words.txt"
ALLOW_PATH = _DICT_DIR / "words_allow.txt"


_KANJI_CLASS = r"[一-鿿]"
_KANJI_LEFT_BOUNDARY = rf"(?<!{_KANJI_CLASS})"
_SINGLE_KANJI_STEM_RE = re.compile(rf"\A{_KANJI_CLASS}\[")


def _with_kanji_left_boundary(pattern: str) -> str:
    if pattern.startswith(("(?<!", "(?!")):
        return pattern
    if _SINGLE_KANJI_STEM_RE.match(pattern) is None:
        return pattern
    return _KANJI_LEFT_BOUNDARY + pattern


def load_patterns(path: pathlib.Path, *, kanji_left_boundary: bool = False) -> list[tuple[re.Pattern[str], str | None]]:
    if not path.is_file():
        return []
    patterns: list[tuple[re.Pattern[str], str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head, sep, tail = stripped.partition("\t")
        replacement = tail if sep else None
        if kanji_left_boundary:
            head = _with_kanji_left_boundary(head)
        try:
            patterns.append((re.compile(head), replacement))
        except re.error:
            continue
    return patterns


def mask_allowed(text: str, allow_patterns: list[tuple[re.Pattern[str], str | None]]) -> str:
    masked = text
    for ap, _ in allow_patterns:
        masked = ap.sub(lambda m: " " * len(m.group(0)), masked)
    return masked


_BLOCKQUOTE_LINE_RE = re.compile(r"(?m)^>.*$")


def mask_blockquote_lines(text: str) -> str:
    return _BLOCKQUOTE_LINE_RE.sub(lambda m: " " * len(m.group(0)), text)


def mask_fenced_code_blocks(text: str) -> str:
    return pyfltr.text.fences.mask_fenced_blocks(text, preserve_columns=True)


_INLINE_CODE_RE = re.compile(r"(`+)([^`\n]+)\1")


def mask_inline_code(text: str) -> str:
    return _INLINE_CODE_RE.sub(lambda m: m.group(1) + " " * len(m.group(2)) + m.group(1), text)


def _mask_all(text: str, allow_patterns: list[tuple[re.Pattern[str], str | None]]) -> str:
    return mask_allowed(mask_inline_code(mask_fenced_code_blocks(mask_blockquote_lines(text))), allow_patterns)


def scan_text(
    text: str,
    deny_patterns: list[tuple[re.Pattern[str], str | None]],
    allow_patterns: list[tuple[re.Pattern[str], str | None]],
) -> list[tuple[int, int, str, str, str | None]]:
    if not deny_patterns:
        return []
    masked = _mask_all(text, allow_patterns)
    hits: list[tuple[int, int, str, str, str | None]] = []
    for dp, replacement in deny_patterns:
        for m in dp.finditer(masked):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_no = text[: m.start()].count("\n") + 1
            col = m.start() - line_start + 1
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            snippet = text[line_start:line_end].rstrip()
            hits.append((line_no, col, m.group(0), snippet, replacement))
    hits.sort(key=lambda h: (h[0], h[1]))
    return hits


def first_hit(
    text: str,
    deny_patterns: list[tuple[re.Pattern[str], str | None]],
    allow_patterns: list[tuple[re.Pattern[str], str | None]],
) -> bool:
    if not deny_patterns:
        return False
    masked = _mask_all(text, allow_patterns)
    return any(dp.search(masked) for dp, _ in deny_patterns)
