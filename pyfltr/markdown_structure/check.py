"""Markdownの文単位の改行と全角丸括弧の入れ子を検出し、改行を挿入して修正する。

CommonMarkの構造（コード、表、引用、リスト）を`markdown-it-py`で判定し、
コード・HTMLコメント・HTMLタグ・表のセル・引用・`逐語引用:`で始まる段落・frontmatterを1文1行の検査から除く。
修正は改行とリスト継続の字下げの追加だけとし、他の文字は変えない。
"""

import dataclasses
import re

import markdown_it
import markdown_it.rules_inline

_COMMENT = re.compile(r"<!--[\s\S]*?(?:-->|$)")
_LIST = re.compile(r"^(\s*)(?:[-+*]|\d+[.)])(\s+)")


@dataclasses.dataclass(frozen=True)
class Issue:
    """違反1件。`offset`は原文の文字位置。"""

    offset: int
    message: str


def _mask(text: str) -> str:
    # 同じ文字数で伏せ、原文への位置対応と行跨ぎの括弧状態を保持する。
    masked = list(text)

    def record_code(state: markdown_it.rules_inline.StateInline, silent: bool) -> bool:
        start, count = state.pos, len(state.tokens)
        accepted = markdown_it.rules_inline.backtick(state, silent)
        if not silent and len(state.tokens) > count and state.tokens[-1].type == "code_inline" and state.src == text:
            masked[start : state.pos] = re.sub(r"[^\r\n]", " ", text[start : state.pos])
        return accepted

    parser = markdown_it.MarkdownIt("commonmark")
    # backtickのエスケープ・区切り長は解析器に任せ、元の位置だけを記録する。
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
            # 文末の強調記法とリンクの閉じ記法は同じ行へ残す。
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


def inspect(text: str) -> tuple[list[Issue], dict[int, str]]:
    """違反の一覧と、自動修正で挿入する位置から字下げへの辞書を返す。"""
    source = _excluded_source(text)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    tokens = markdown_it.MarkdownIt("commonmark").enable("table").parse(source)
    issues: list[Issue] = []
    insertions: dict[int, str] = {}
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
        # cellのinline.mapは同じ物理行を共有するため、括弧の深さは表全体で一度だけ更新する。
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
            issues.append(Issue(position - 1, "1文1行へ改行する（自動修正の対象）"))
            # 空白は原文のまま次行へ送り、リストの継続に必要な字下げだけを追加する。
            insertions[position] = indent
    return sorted(issues, key=lambda issue: issue.offset), insertions
