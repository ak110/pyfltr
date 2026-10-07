"""各ツールが報告する診断の共通型。"""

import dataclasses

FILE_PATTERN = r"(?:[A-Za-z]:)?[^\s:]+"


@dataclasses.dataclass
class ErrorLocation:
    """エラー箇所の情報。"""

    file: str
    line: int
    col: int | None
    """診断の開始列。原則として1起点の行内位置である。

    textlintは文を切り出すライブラリを用いるルールで行内位置にならない場合がある。
    """
    command: str
    message: str
    rule: str | None = None
    """ルールコード（F401, C0114, SC2086等）"""
    severity: str | None = None
    """診断の重要度（"error" | "warning" | "info"）"""
    fix: str | None = None
    """自動修正の適用可能性（"safe" | "unsafe" | "suggested" | "none"）

    `None`はツールが自動修正情報を返さないことを示し、JSON Lines出力でも省略する。
    `"none"`はツールが自動修正情報を返した上で「自動修正不可」と明示した場合に使う。
    """
    rule_url: str | None = None
    """ルールドキュメントのURL（Noneは未対応ツールまたはrule未設定時）"""
    hint: str | None = None
    """診断メッセージに添える短い修正ヒント（Noneはヒント未登録のルール）。

    JSON Lines出力では`command.hints`辞書にrule→ヒント文字列として集約される。
    messages[]要素への個別出力は行わない。
    """
    end_line: int | None = None
    """診断範囲の最終行。終了位置を出力するツールで設定される。

    範囲の最終行を含む値である。ツールが返す終了位置は範囲末尾の次の位置を指す場合があり、
    範囲が行末で終わると次行の行番号が渡される。終了列が次行の先頭（1起点で1）を指し、
    終了行が開始行より後にある場合、`_to_inclusive_end_position`が終了行を直前の行へ補正して格納する。
    """
    end_col: int | None = None
    """診断範囲の終了列。原則として1起点・終端排他の行内位置である。

    `_to_inclusive_end_position`が終了行を直前の行へ補正する場合、直前行の終端列は
    ツール出力だけでは算出できないため`None`へ正規化する。
    textlintは文を切り出すライブラリを用いるルールで行内位置にならない場合がある。
    """
