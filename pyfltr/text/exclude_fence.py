"""Markdown見出し配下のフェンス内側行をマスクする。"""

import re

_H2_HEADING_RE = re.compile(r"^##\s+")
_FENCE_LINE_RE = re.compile(r"^(`{3,}|~{3,})")


def mask_fenced_blocks_under_headings(text: str, headings: list[str]) -> str:
    """指定H2見出し配下のフェンス内側行を空行へ置換する。

    フェンス区切り行（``` / ~~~）は保持し、内側行は改行のみを残す。
    改行数が保存されるため、markdownlint・textlint診断の行番号は元ファイル基準となる。
    行内文字数は保存しないため、markdownlint MD013 line-length違反の発火を防ぐ。
    未閉じフェンスの改行なしEOF最終行は短い空白へ置換して長さ由来ルールの発火を防ぐ。
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
            out.append(tail if tail else " ")

    return "".join(out)
