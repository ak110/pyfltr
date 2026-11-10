"""textlintの診断と位置情報の解析。"""

import typing

import pyfltr.paths
import pyfltr.tools
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import (
    normalize_severity,
    parse_file_messages_format,
    parse_with_pattern,
    to_inclusive_end_position,
    try_json_loads,
)


def parse_textlint_json(output: str) -> list[ErrorLocation]:
    """Textlint --format json出力をパース。JSON解析失敗時はregexにフォールバック。

    出力構造はESLintと同じfilePath + messages配列形式。
    textlintはルールによって複数行にわたるmessage（sentence-lengthの`Over X characters.`等）を返すため、
    JSONL `messages[].msg`は1行に保つ目的で改行を半角スペースへ畳む。
    """
    data = try_json_loads(output)
    if not isinstance(data, list):
        return parse_with_pattern("textlint", output, pyfltr.tools.BUILTIN_COMMANDS["textlint"].require_diagnostic_pattern())

    def _msg_to_location(msg: dict, file_path: str) -> ErrorLocation | None:
        line = msg.get("line")
        if not isinstance(line, int):
            return None
        raw_col = msg.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        rule_id = str(msg.get("ruleId") or "")
        # textlintはJSON出力でautofixの有無を明示する。
        fix_value = "safe" if msg.get("fix") else "none"
        rule = rule_id or None
        hint = pyfltr.tools.BUILTIN_COMMANDS["textlint"].rule_hints.get(rule_id) if rule_id else None
        # textlint側のmsgは複数行になり得るため、JSONL `messages[].msg`では空白へ畳む。
        # 範囲表記`(L17:1〜23)`を末尾へ視認しやすく追加する都合上、先に1行化しておく必要がある。
        message = normalize_whitespace(str(msg.get("message", "")))
        end_line, end_col = extract_textlint_end_position(msg.get("loc"))
        # sentence-length違反では文の起点・終点が分からないと修正しづらいため、
        # textlint v12+が返す`loc`フィールドから範囲表記を組み立てて末尾に併記する。
        # 他ルールでは違反箇所自体が短く、併記が冗長になるため対象外。
        if rule_id == "ja-technical-writing/sentence-length":
            range_text = format_textlint_loc(msg.get("loc"))
            if range_text:
                message = f"{message} {range_text}"
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        return ErrorLocation(
            file=pyfltr.paths.normalize_separators(file_path),
            line=line,
            col=col,
            command="textlint",
            message=message,
            rule=rule,
            severity=normalize_severity(msg.get("severity")),
            fix=fix_value,
            hint=hint,
            end_line=end_line,
            end_col=end_col,
        )

    return parse_file_messages_format(data, _msg_to_location)


def extract_textlint_loc_positions(
    loc: typing.Any,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Textlintの`loc`から`start`/`end`の`(line, col)`ペアを独立に取り出す。

    片方のみが有効な`loc`にも対応するため、`start`と`end`は独立に検証する
    （古いtextlintや一部ルールが`end`のみを返すケースでも有効値を失わないため）。
    `loc`不在・形式不一致は`(None, None)`を返す。
    """
    if not isinstance(loc, dict):
        return None, None
    return extract_textlint_point(loc.get("start")), extract_textlint_point(loc.get("end"))


def extract_textlint_point(point: typing.Any) -> tuple[int, int] | None:
    """Textlintの`{"line": int, "column": int}`形式から`(line, col)`を取り出す。"""
    if not isinstance(point, dict):
        return None
    line = point.get("line")
    col = point.get("column")
    if not isinstance(line, int) or not isinstance(col, int):
        return None
    return line, col


def extract_textlint_end_position(loc: typing.Any) -> tuple[int | None, int | None]:
    """Textlintの`loc.end`から`(end_line, end_col)`を取り出す。

    `loc`不在・形式不一致は`(None, None)`を返す（古いtextlintへの後方互換）。
    取り出した`end_line`は呼び出し側で包含的な最終行へ正規化する。
    行を補正する場合は異なる行を指す`end_col`を`None`にし、それ以外は終端排他のまま
    `ErrorLocation`へ格納する。
    """
    _, end = extract_textlint_loc_positions(loc)
    if end is None:
        return None, None
    return end


def format_textlint_loc(loc: typing.Any) -> str:
    """Textlintの`loc`フィールドから`(L17:1〜23)`形式の範囲文字列を組み立てる。

    1行内で完結する場合は`(Lstart:start_col〜end_col)`、
    複数行にまたがる場合は`(Lstart:start_col〜Lend:end_col)`を返す。
    `start`/`end`のいずれかが欠けている場合は空文字列を返す（古いtextlintや未提供ルールへの後方互換）。
    """
    start, end = extract_textlint_loc_positions(loc)
    if start is None or end is None:
        return ""
    start_line, start_col = start
    end_line, end_col = end
    if start_line == end_line:
        return f"(L{start_line}:{start_col}〜{end_col})"
    return f"(L{start_line}:{start_col}〜L{end_line}:{end_col})"


def normalize_whitespace(text: str) -> str:
    """連続するホワイトスペース（改行・タブ・全角空白等）を半角スペース1つに畳んで前後を取り除く。

    JSONL `messages[].msg`を1行に保つ用途で使う。`re.split`をそのまま結合するため、
    複数行に分かれたmsgを意味単位として連結したいケースにも適合する。
    """
    return " ".join(text.split())
