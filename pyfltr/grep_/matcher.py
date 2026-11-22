import pathlib
import re


def compile_pattern(
    patterns: list[str],
    *,
    fixed_strings: bool,
    ignore_case: bool,
    smart_case: bool,
    word_regexp: bool,
    line_regexp: bool,
    multiline: bool,
) -> re.Pattern[str]:
    if not patterns:
        raise ValueError("パターンが指定されていません")
    prepared = [re.escape(p) for p in patterns] if fixed_strings else list(patterns)
    combined = "|".join(f"(?:{p})" for p in prepared)
    if word_regexp:
        combined = rf"\b(?:{combined})\b"
    if line_regexp:
        combined = rf"(?:^(?:{combined})$)"
    flags = 0
    effective_ignore_case = ignore_case
    if smart_case and not ignore_case:
        effective_ignore_case = not any(ch.isupper() for ch in combined)
    if effective_ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL | re.MULTILINE
    elif line_regexp:
        flags |= re.MULTILINE
    try:
        return re.compile(combined, flags)
    except re.error as exc:
        pos_text = f"（位置 {exc.pos}）" if exc.pos is not None else ""
        raise ValueError(f"正規表現のコンパイルに失敗しました{pos_text}: {exc}") from exc


def read_pattern_file(path: pathlib.Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line]
