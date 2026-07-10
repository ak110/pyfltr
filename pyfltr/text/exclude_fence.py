"""Markdown見出し配下のフェンス内側行をマスクする。"""

import re

_H2_HEADING_RE = re.compile(r"^##\s+")
_FENCE_LINE_RE = re.compile(r"^(`{3,}|~{3,})")


def mask_fenced_blocks_under_headings(text: str, headings: list[str]) -> str:
    """指定H2見出し配下のフェンス内側行を同長空白へ置換する。

    `pyfltr.colloquial.check.mask_fenced_code_blocks` と同じ置換方式を使うが、
    対象範囲を指定H2見出し配下に限定する。
    """
    if not text or not headings:
        return text

    heading_set = set(headings)
    out: list[str] = []
    in_target_section = False
    in_fence = False
    fence_char = ""
    fence_len = 0

    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n").rstrip("\r")
        tail = line[len(body) :]
        if not in_fence and _H2_HEADING_RE.match(body):
            in_target_section = body.strip() in heading_set

        if not in_fence:
            match = _FENCE_LINE_RE.match(body)
            if in_target_section and match is not None:
                marker = match.group(1)
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            out.append(line)
            continue

        close = re.match(rf"^{re.escape(fence_char)}{{{fence_len},}}\s*$", body)
        if close is not None:
            in_fence = False
            fence_char = ""
            fence_len = 0
            out.append(line)
        else:
            out.append(" " * len(body) + tail)

    return "".join(out)
