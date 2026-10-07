"""Markdown見出し配下のフェンス内側行をマスクする。"""

import pyfltr.text.fences


def mask_fenced_blocks_under_headings(text: str, headings: list[str]) -> str:
    """指定H2見出し配下のフェンス内側行を空行へ置換する。

    フェンス区切り行（``` / ~~~）は保持し、内側行は改行のみを残す。
    改行数が保存されるため、markdownlint・textlint診断の行番号は元ファイル基準となる。
    行内文字数は保存しないため、markdownlint MD013 line-length違反の発火を防ぐ。
    未閉じフェンスの改行なしEOF最終行は短い空白へ置換して長さ由来ルールの発火を防ぐ。
    """
    return pyfltr.text.fences.mask_fenced_blocks(text, headings=headings)
