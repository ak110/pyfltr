"""Markdownフェンスの内側を、行位置または文字位置を保ってマスクする。"""

import re

_H2_HEADING_RE = re.compile(r"^##\s+")
_FENCE_LINE_RE = re.compile(r"^(`{3,}|~{3,})")


def mask_fenced_blocks(
    text: str,
    *,
    headings: list[str] | None = None,
    preserve_columns: bool = False,
) -> str:
    """対象のフェンス内側を空白または改行へ置き換える。

    headingsがNoneなら全ブロック、指定時はそのH2見出し配下だけを対象にする。
    開閉マーカーは保持し、終了は開始と同種かつ同長以上のマーカーに限る。
    preserve_columnsが真なら文字数を保ち、偽なら長さ由来の診断を避ける。
    """
    if not text or headings == []:
        return text
    heading_set = set(headings) if headings is not None else None
    in_target_section = headings is None
    in_fence = False
    fence_char = ""
    fence_len = 0
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n").rstrip("\r")
        tail = line[len(body) :]
        if not in_fence and heading_set is not None and _H2_HEADING_RE.match(body):
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
        elif preserve_columns:
            out.append(" " * len(body) + tail)
        else:
            out.append(tail if tail else " ")
    return "".join(out)
