import bisect
import dataclasses
import re

import markdown_it
import markdown_it.rules_inline
import markdown_it.token

_COMMENT = re.compile(r"<!--[\s\S]*?(?:-->|$)")
_LIST = re.compile(r"^(\s*)(?:[-+*]|\d+[.)])(\s+)")
_TEMPLATE_DELIMITERS = (("{%", "%}"), ("{{", "}}"), ("{#", "#}"))


@dataclasses.dataclass(frozen=True)
class Issue:
    offset: int
    message: str


def _mask(text: str) -> str:
    masked = list(text)

    def record_code(state: markdown_it.rules_inline.StateInline, silent: bool) -> bool:
        start, count = state.pos, len(state.tokens)
        accepted = markdown_it.rules_inline.backtick(state, silent)
        if not silent and len(state.tokens) > count and state.tokens[-1].type == "code_inline" and state.src == text:
            masked[start : state.pos] = re.sub(r"[^\r\n]", " ", text[start : state.pos])
        return accepted

    parser = markdown_it.MarkdownIt("commonmark")
    parser.inline.ruler.at("backticks", record_code)
    parser.inline.parse(text, parser, {}, [])
    text = "".join(masked)
    for pattern in (_COMMENT, re.compile(r"<[^>\n]+>")):
        text = pattern.sub(lambda m: re.sub(r"[^\r\n]", " ", m[0]), text)
    return text


def _excluded_source(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip("\ufeff\r\n") == "---":
        for end in range(1, len(lines)):
            if lines[end].strip() in {"---", "..."}:
                lines[: end + 1] = [re.sub(r"[^\r\n]", " ", line) for line in lines[: end + 1]]
                break
    source = "".join(lines)
    if source.startswith("\ufeff"):
        source = " " + source[1:]
    return _COMMENT.sub(lambda m: re.sub(r"[^\r\n]", " ", m[0]), source)


def _sentence_breaks(text: str) -> list[int]:
    masked = _mask(text)
    stack: list[str] = []
    pairs = {"（": "）", "(": ")", "「": "」", "『": "』"}
    breaks = []
    for index, char in enumerate(masked):
        if char in pairs:
            stack.append(pairs[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char == "。" and not stack:
            end = index + 1
            while end < len(text):
                closing = re.match(r"\*\*|__|~~|\*|_", text[end:])
                if closing and text[:end].count(closing[0]) % 2:
                    end += len(closing[0])
                    continue
                if text[end:].startswith("]("):
                    link_end = text.find(")", end + 2)
                    if link_end >= 0:
                        end = link_end + 1
                        continue
                break
            line_end = masked.find("\n", end)
            if line_end < 0:
                line_end = len(masked)
            if text[end:line_end].strip():
                breaks.append(end)
    return breaks


def _safe_insertions(
    parser: markdown_it.MarkdownIt, text: str, candidates: dict[int, str], newline: str
) -> tuple[dict[int, str], list[Issue]]:
    baseline = _structure(parser.parse(text))
    accepted: dict[int, str] = {}
    issues: list[Issue] = []
    for position, indent in sorted(candidates.items(), reverse=True):
        candidate = text[:position] + newline + indent + text[position:]
        if text[position:].lstrip(" \t").startswith("#"):
            message = "1文1行へ改行する（行頭の # が見出しとして扱われうるため、文を調整して手動で改行する）"
        elif _structure(parser.parse(candidate)) != baseline:
            message = "1文1行へ改行する（改行するとMarkdownの構造が変わるため、文を調整して手動で改行する）"
        else:
            message = "1文1行へ改行する（自動修正の対象）"
            accepted[position] = indent
            text = candidate
        issues.append(Issue(position - 1, message))
    return accepted, issues


def _structure(tokens: list[markdown_it.token.Token]) -> list[tuple[str, str, int, bool]]:
    return [(token.type, token.tag, token.nesting, token.hidden) for token in tokens]


def _check_insertions(
    parser: markdown_it.MarkdownIt, text: str, candidates: dict[int, str], offsets: list[int]
) -> tuple[dict[int, str], list[Issue]]:
    baseline = parser.parse(text)
    positions = sorted(candidates)
    newline = "\r\n" if "\r\n" in text else "\n"
    accepted: dict[int, str] = {}
    issues: list[Issue] = []
    for token in baseline:
        if token.level or token.map is None:
            continue
        start_line, stop_line = token.map
        start, stop = offsets[start_line], offsets[stop_line]
        left, right = bisect.bisect_left(positions, start), bisect.bisect_left(positions, stop)
        block_candidates = {position - start: candidates[position] for position in positions[left:right]}
        if not block_candidates:
            continue
        block_insertions, block_issues = _safe_insertions(parser, text[start:stop], block_candidates, newline)
        accepted.update((position + start, indent) for position, indent in block_insertions.items())
        issues.extend(Issue(issue.offset + start, issue.message) for issue in block_issues)

    if len(issues) == len(candidates):
        parts: list[str] = []
        previous = 0
        for position, indent in sorted(accepted.items()):
            parts.extend((text[previous:position], newline, indent))
            previous = position
        parts.append(text[previous:])
        if _structure(parser.parse("".join(parts))) == _structure(baseline):
            return accepted, issues
    return _safe_insertions(parser, text, candidates, newline)


def _has_template(text: str) -> bool:
    # 閉じ記号を伴う区切りだけをJinja2構文とみなし、記法例の開始記号だけを含む通常の文書は対象に残す。
    # 最初の開始記号より後に閉じ記号があるかだけを調べ、開始記号ごとの再走査を避ける。
    for opening, closing in _TEMPLATE_DELIMITERS:
        start = text.find(opening)
        if start >= 0 and text.find(closing, start + len(opening)) >= 0:
            return True
    return False


def inspect(text: str) -> tuple[list[Issue], dict[int, str]]:
    source = _excluded_source(text)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    parser = markdown_it.MarkdownIt("commonmark").enable("table")
    tokens = parser.parse(source)
    # 空白制御や条件分岐で、原文では別のブロックにあるテキストも展開後に連結しうる。
    has_template = _has_template(text)
    issues: list[Issue] = []
    candidates: dict[int, str] = {}
    quote_depth = 0
    paragraph = False
    in_table = False
    lists: list[str] = []
    depth = 0
    for token in tokens:
        if token.type == "blockquote_open":
            quote_depth += 1
        elif token.type == "blockquote_close":
            quote_depth -= 1
        elif token.type == "list_item_open":
            assert token.map is not None
            first = lines[token.map[0]].rstrip("\r\n")
            match = _LIST.match(first)
            lists.append(" " * match.end() if match else "")
        elif token.type == "list_item_close":
            lists.pop()
        elif token.type == "paragraph_open":
            paragraph = True
        elif token.type == "paragraph_close":
            paragraph = False
        elif token.type == "table_open":
            in_table = True
        elif token.type == "table_close":
            in_table = False
        if token.type == "inline" and in_table:
            continue
        if token.type not in {"inline", "table_open"} or token.map is None or quote_depth:
            continue
        start, stop = token.map
        raw = "".join(lines[start:stop])
        masked = _mask(raw)
        for index, char in enumerate(masked):
            if char == "（":
                if depth:
                    issues.append(Issue(offsets[start] + index, "全角丸括弧の入れ子を解消する"))
                depth += 1
            elif char == "）":
                depth = max(0, depth - 1)
        if token.type != "inline" or not paragraph or token.content.lstrip().startswith("逐語引用:"):
            continue
        first_content = token.content.split("\n", maxsplit=1)[0]
        content_column = lines[start].find(first_content)
        indent = " " * content_column if lists and content_column >= 0 else (lists[-1] if lists else "")
        for end in _sentence_breaks(raw):
            position = offsets[start] + end
            if has_template:
                issues.append(Issue(position - 1, "1文1行へ改行する（Jinja2構文を含むため、展開結果を確認して手動で改行する）"))
            else:
                candidates[position] = indent
    if candidates:
        insertions, break_issues = _check_insertions(parser, text, candidates, offsets)
        issues.extend(break_issues)
    else:
        insertions = {}
    return sorted(issues, key=lambda issue: issue.offset), insertions
