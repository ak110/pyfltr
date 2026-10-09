"""ツール固有の診断解析。"""

import json
import re
import typing

import pyfltr.link_checks
import pyfltr.paths
import pyfltr.rule_urls
import pyfltr.tools
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import (
    json_int,
    json_list_field,
    normalize_severity,
    one_based_offset,
    parse_file_messages_format,
    parse_with_pattern,
    to_inclusive_end_position,
    try_json_loads,
)

_AUDIT_SEVERITY_MAP: dict[str, str] = {
    "critical": "error",
    "high": "error",
    "moderate": "warning",
    "low": "warning",
    "info": "info",
}


def normalize_audit_severity(value: typing.Any) -> str | None:
    """npm系auditツールの深刻度ラベルを`"error"` / `"warning"` / `"info"`へ正規化する。

    未知の値やNoneは`None`を返し、JSONL出力側で省略される。
    """
    if not isinstance(value, str):
        return None
    return _AUDIT_SEVERITY_MAP.get(value.strip().lower())


def parse_eslint_json(output: str) -> list[ErrorLocation]:
    """ESLint --format json出力をパース。

    ESLint 9系以降でcompact / unixなどのコアフォーマッタが除去されたため、
    pyfltrでは`--format json`を使う。出力は以下のような配列。

    [
      {
        "filePath": "/abs/src/foo.js",
        "messages": [
          {"line": 10, "column": 5, "message": "...", "ruleId": "no-unused-vars", "severity": 2}
        ]
      }
    ]

    stderr混入等でパースに失敗した場合は空リストを返す（regexパーサーが
    マッチしない時の挙動と揃える）。
    """
    data = try_json_loads(output)
    if not isinstance(data, list):
        return []

    def _msg_to_location(msg: dict, file_path: str) -> ErrorLocation | None:
        line = msg.get("line")
        if not isinstance(line, int):
            return None
        raw_col = msg.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        end_line = json_int(msg.get("endLine"))
        end_col = json_int(msg.get("endColumn"))
        rule_id = str(msg.get("ruleId") or "")
        text = str(msg.get("message", ""))
        message = f"{text} ({rule_id})" if rule_id else text
        # ESLintのJSONはautofixがある場合のみ`fix`オブジェクトが付与される。
        # 自動修正情報の有無を報告するツールなので、欠落時は`"none"`として
        # 「自動修正不可」を明示する（`None`省略との区別を維持）。
        fix_value = "safe" if msg.get("fix") else "none"
        rule = rule_id or None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        return ErrorLocation(
            file=pyfltr.paths.normalize_separators(file_path),
            line=line,
            col=col,
            command="eslint",
            message=message.strip(),
            rule=rule,
            severity=normalize_severity(msg.get("severity")),
            fix=fix_value,
            rule_url=pyfltr.rule_urls.build_rule_url("eslint", rule),
            end_line=end_line,
            end_col=end_col,
        )

    return parse_file_messages_format(data, _msg_to_location)


def parse_ruff_check_json(output: str) -> list[ErrorLocation]:
    """Ruff check --output-format=json出力をパース。JSON解析失敗時はregexにフォールバック。"""
    data = try_json_loads(output)
    if not isinstance(data, list):
        return parse_with_pattern(
            "ruff-check", output, pyfltr.tools.BUILTIN_COMMANDS["ruff-check"].require_diagnostic_pattern()
        )
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        loc = entry.get("location", {})
        if not isinstance(loc, dict):
            continue
        line = loc.get("row")
        if not isinstance(line, int):
            continue
        raw_col = loc.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        end_loc = entry.get("end_location")
        end_line = json_int(end_loc.get("row")) if isinstance(end_loc, dict) else None
        end_col = json_int(end_loc.get("column")) if isinstance(end_loc, dict) else None
        fix_obj = entry.get("fix")
        # ruffは自動修正情報の有無を明示的に返すツール。`fix`欠落時は
        # 自動修正不可として`"none"`を出力する。
        fix_value: str | None = str(fix_obj.get("applicability", "safe")) if isinstance(fix_obj, dict) else "none"
        rule = str(entry.get("code", "")) or None
        entry_url = entry.get("url")
        existing_url = str(entry_url) if isinstance(entry_url, str) and entry_url else None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("filename", ""))),
                line=line,
                col=col,
                command="ruff-check",
                message=str(entry.get("message", "")),
                rule=rule,
                severity=normalize_severity(entry.get("severity")) or "error",
                fix=fix_value,
                rule_url=pyfltr.rule_urls.build_rule_url("ruff-check", rule, existing_url=existing_url),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_pylint_pattern(output: str) -> list[ErrorLocation]:
    """Pylintのテキスト出力を解析し、0起点列を1起点へ変換する。"""
    results = parse_with_pattern("pylint", output, pyfltr.tools.BUILTIN_COMMANDS["pylint"].require_diagnostic_pattern())
    for result in results:
        result.col = one_based_offset(result.col)
    return results


def parse_pylint_json(output: str) -> list[ErrorLocation]:
    """Pylint --output-format=json2出力をパース。JSON解析失敗時はregexにフォールバック。

    公式ドキュメントURLが`symbol`基準（`missing-module-docstring`等）のため、
    `ErrorLocation.rule`には`symbol`を格納する。`messageId`（`C0114`等）は
    `ErrorLocation.message`の先頭に付与して保持する。
    """
    data = try_json_loads(output)
    if not isinstance(data, dict) or "messages" not in data:
        return parse_pylint_pattern(output)
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        return parse_pylint_pattern(output)

    # pylintのmessagesはファイルごとにネストしない平坦な配列のため、直接ループしてErrorLocationを構築する。
    results: list[ErrorLocation] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        line = msg.get("line")
        if not isinstance(line, int):
            continue
        col = one_based_offset(msg.get("column"))
        end_line = json_int(msg.get("endLine"))
        end_col = one_based_offset(msg.get("endColumn"))
        msg_type = str(msg.get("type", "")).lower()
        severity = "error" if msg_type in ("error", "fatal") else "warning"
        symbol = str(msg.get("symbol") or "") or None
        message_id = str(msg.get("messageId") or "")
        original_message = str(msg.get("message", ""))
        # 既存ruleスキーマ（機械判別可能な識別子）とmessageIdの両方をJSONL上に残す。
        combined_message = f"{message_id}: {original_message}" if message_id else original_message
        # 公式ドキュメントURLはカテゴリー名（`convention` / `warning` / `error` / `refactor` /
        # `information` / `fatal`）を必要とする。`type`フィールドをそのまま渡す。
        category = msg_type or None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(msg.get("path", ""))),
                line=line,
                col=col,
                command="pylint",
                message=combined_message,
                rule=symbol,
                severity=severity,
                rule_url=pyfltr.rule_urls.build_rule_url("pylint", symbol, category=category),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_pyright_json(output: str) -> list[ErrorLocation]:
    """Pyright --outputjson出力をパース。JSON解析失敗時はregexにフォールバック。"""
    data = try_json_loads(output)
    if not isinstance(data, dict) or "generalDiagnostics" not in data:
        return parse_with_pattern("pyright", output, pyfltr.tools.BUILTIN_COMMANDS["pyright"].require_diagnostic_pattern())
    diags = data.get("generalDiagnostics", [])
    if not isinstance(diags, list):
        return parse_with_pattern("pyright", output, pyfltr.tools.BUILTIN_COMMANDS["pyright"].require_diagnostic_pattern())
    results: list[ErrorLocation] = []
    for diag in diags:
        if not isinstance(diag, dict):
            continue
        range_obj = diag.get("range", {})
        if not isinstance(range_obj, dict):
            continue
        start = range_obj.get("start", {})
        if not isinstance(start, dict):
            continue
        # pyrightのline/characterは0-based
        line = start.get("line")
        if not isinstance(line, int):
            continue
        raw_char = start.get("character")
        col = (raw_char + 1) if isinstance(raw_char, int) else None
        end = range_obj.get("end")
        end_line = one_based_offset(end.get("line")) if isinstance(end, dict) else None
        end_col = one_based_offset(end.get("character")) if isinstance(end, dict) else None
        rule = str(diag.get("rule", "")) or None
        end_line, end_col = to_inclusive_end_position(line + 1, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(diag.get("file", ""))),
                line=line + 1,
                col=col,
                command="pyright",
                message=str(diag.get("message", "")),
                rule=rule,
                severity=normalize_severity(diag.get("severity")),
                rule_url=pyfltr.rule_urls.build_rule_url("pyright", rule),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_arid_json(output: str, *, resolve_path: typing.Callable[[str], str] | None = None) -> list[ErrorLocation]:
    """aridのJSON出力に含まれる全重複位置を診断へ変換する。

    `resolve_path`は`pyfltr.parsing.entry.parse_errors`が渡すパス変換関数で、
    ツール出力のパスを起点相対へ変換する。診断の`file`に加えてメッセージ内の他の重複位置にも
    同じ値を使うため、本パーサーは変換関数を受け取って自ら適用する。省略時は区切りの統一だけを行う。

    `findings`の各要素が`lines`・`occurrences`・`context`・`scope`・`distribution`・`locations`を
    必ず持つことを前提とする（arid 2.2.2のreport schema_version 4で実測）。
    致命エラー時のaridは`findings`を持たない別スキーマ（`schema_version` 1）を返すため、
    本関数は空リストを返し、生出力が`CommandResult.message`へ入る経路へ委ねる。
    `end_line`はaridが返す包含の最終行であり、`to_inclusive_end_position`は値を変えない。
    """
    findings = json_list_field(output, "findings")
    if findings is None:
        return []
    results: list[ErrorLocation] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        lines = json_int(finding.get("lines"))
        occurrences = json_int(finding.get("occurrences"))
        context = finding.get("context")
        scope = finding.get("scope")
        distribution = finding.get("distribution")
        if (
            lines is None
            or occurrences is None
            or not isinstance(context, str)
            or not isinstance(scope, str)
            or not isinstance(distribution, str)
        ):
            continue
        raw_locations = finding.get("locations")
        if not isinstance(raw_locations, list):
            continue
        locations: list[tuple[str, int, int]] = []
        for location in raw_locations:
            if not isinstance(location, dict):
                continue
            path = location.get("path")
            start_line = json_int(location.get("start_line"))
            end_line = json_int(location.get("end_line"))
            if isinstance(path, str) and path and start_line is not None and end_line is not None:
                resolved = resolve_path(path) if resolve_path is not None else pyfltr.paths.normalize_separators(path)
                locations.append((resolved, start_line, end_line))
        for index, (path, start_line, raw_end_line) in enumerate(locations):
            other_locations = [
                f"{other_path}:{other_start}-{other_end}"
                for other_index, (other_path, other_start, other_end) in enumerate(locations)
                if other_index != index
            ]
            message = f"{lines} duplicated lines ({context}/{scope}, {distribution}, {occurrences} occurrences)"
            if other_locations:
                message += f"; other locations: {', '.join(other_locations)}"
            end_line, end_col = to_inclusive_end_position(start_line, raw_end_line, None)
            results.append(
                ErrorLocation(
                    file=path,
                    line=start_line,
                    col=None,
                    command="arid",
                    message=message,
                    rule=str(finding.get("code", "")) or None,
                    severity="error",
                    rule_url=None,
                    end_line=end_line,
                    end_col=end_col,
                )
            )
    return results


def parse_shellcheck_json(output: str) -> list[ErrorLocation]:
    """Shellcheck -f json出力をパース。JSON解析失敗時はregexにフォールバック。"""
    data = try_json_loads(output)
    if not isinstance(data, list):
        return parse_with_pattern(
            "shellcheck", output, pyfltr.tools.BUILTIN_COMMANDS["shellcheck"].require_diagnostic_pattern()
        )
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        line = entry.get("line")
        if not isinstance(line, int):
            continue
        raw_col = entry.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        end_line = json_int(entry.get("endLine"))
        end_col = json_int(entry.get("endColumn"))
        code = entry.get("code")
        rule = f"SC{code}" if isinstance(code, int) else None
        # shellcheckはJSON出力で自動修正情報の有無を明示する。
        fix_value = "safe" if entry.get("fix") else "none"
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("file", ""))),
                line=line,
                col=col,
                command="shellcheck",
                message=str(entry.get("message", "")),
                rule=rule,
                severity=normalize_severity(entry.get("level")),
                fix=fix_value,
                rule_url=pyfltr.rule_urls.build_rule_url("shellcheck", rule),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


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


def parse_typos_jsonl(output: str) -> list[ErrorLocation]:
    """Typos --format=json出力をパース（JSON Lines形式）。解析失敗時はregexにフォールバック。"""
    results: list[ErrorLocation] = []
    any_parsed = False
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        # typosのJSONエントリにはtypeフィールドがある。typo以外（binary等）はスキップ
        if entry.get("type") not in ("typo", None):
            continue
        any_parsed = True
        line_num = entry.get("line_num")
        if not isinstance(line_num, int):
            continue
        typo = str(entry.get("typo", ""))
        corrections = entry.get("corrections", [])
        if isinstance(corrections, list) and corrections:
            correction_str = ", ".join(str(c) for c in corrections)
            message = f"`{typo}` -> `{correction_str}`"
            fix_value: str | None = "safe"
        else:
            # typosは自動修正候補の有無を明示的に返すため、候補なしは`"none"`。
            message = f"`{typo}`"
            fix_value = "none"
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("path", ""))),
                line=line_num,
                col=None,
                command="typos",
                message=message,
                severity="warning",
                fix=fix_value,
            )
        )
    if not any_parsed and output.strip():
        return parse_with_pattern("typos", output, pyfltr.tools.BUILTIN_COMMANDS["typos"].require_diagnostic_pattern())
    return results


def parse_pinact_sarif(output: str) -> list[ErrorLocation]:
    """`pinact run --format sarif`のSARIF出力をパースする。

    出力例::

        {
          "runs": [
            {
              "results": [
                {
                  "ruleId": "parse-error",
                  "level": "error",
                  "message": {"text": "failed to handle a line: action can't be pinned"},
                  "locations": [
                    {"physicalLocation": {"artifactLocation": {"uri": ".github/workflows/ci.yaml"},
                                          "region": {"startLine": 7}}}
                  ]
                }
              ]
            }
          ]
        }

    pinactは標準エラーへ人間向けの行を書き、pyfltrは標準エラーを標準出力へ合流させるため、
    SARIFの前に別の行が並ぶ。JSON本体は`try_json_loads`で取り出す。
    JSON解析失敗時は空リストを返す。
    """
    runs = json_list_field(output, "runs")
    if runs is None:
        return []
    results: list[ErrorLocation] = []
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("results"), list):
            continue
        for entry in run["results"]:
            if not isinstance(entry, dict):
                continue
            locations = entry.get("locations")
            if not isinstance(locations, list) or not locations or not isinstance(locations[0], dict):
                continue
            physical = locations[0].get("physicalLocation")
            if not isinstance(physical, dict):
                continue
            artifact = physical.get("artifactLocation")
            region = physical.get("region")
            if not isinstance(artifact, dict) or not isinstance(region, dict):
                continue
            line = json_int(region.get("startLine"))
            if line is None:
                continue
            message = entry.get("message")
            rule = str(entry.get("ruleId", "") or "") or None
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(str(artifact.get("uri", ""))),
                    line=line,
                    col=json_int(region.get("startColumn")),
                    command="pinact",
                    message=str(message.get("text", "") or "") if isinstance(message, dict) else "",
                    rule=rule,
                    severity=normalize_severity(entry.get("level")),
                )
            )
    return results


def parse_designmd_json(output: str) -> list[ErrorLocation]:
    """`@google/design.md lint`のJSON出力をパースする。

    出力例::

        {
          "findings": [
            {
              "severity": "warning",
              "path": "components.button-primary",
              "message": "..."
            }
          ],
          "summary": {"errors": 0, "warnings": 1, "info": 1}
        }

    `path`はDESIGN.md内のJSONパス（プロパティ参照）であり、ファイルシステムのパスではない。
    対象ファイル名は仕様上`DESIGN.md`固定のため、`ErrorLocation.file`にはそれを格納し、
    JSONパスは`message`先頭へ併記する。`line`は仕様上提供されないため`0`を格納する。
    JSON解析失敗時は空リストを返す。
    """
    findings = json_list_field(output, "findings")
    if findings is None:
        return []
    results: list[ErrorLocation] = []
    for entry in findings:
        if not isinstance(entry, dict):
            continue
        json_path = str(entry.get("path", "") or "")
        text = str(entry.get("message", "") or "")
        message = f"{json_path}: {text}" if json_path else text
        results.append(
            ErrorLocation(
                file="DESIGN.md",
                line=0,
                col=None,
                command="designmd",
                message=message,
                severity=normalize_severity(entry.get("severity")),
            )
        )
    return results


def parse_lychee_json(output: str) -> list[ErrorLocation]:
    """`lychee --format json`のJSON出力をパースする。

    出力例::

        {
          "total": 100, "successful": 80, ...,
          "error_map": {
            "src/foo.md": [
              {
                "url": "https://example.com/dead",
                "status": {"text": "Error: 404 - Not Found", "code": 404},
                ...
              }
            ]
          }
        }

    `error_map`と`timeout_map`は「ファイルパス → 失敗レスポンス配列」のmap。各失敗から`url`/`status.text`を抽出し、
    `ErrorLocation.message`へ整形する。本パーサーは`line=1`固定とし、JSONの`span`による位置を取り込まない。
    HTTP(S)の5xxと応答タイムアウトだけをwarning、その他をerrorとして取り込む。
    JSON解析失敗時は空リストを返す。
    """
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return []
    error_map = data.get("error_map", {})
    timeout_map = data.get("timeout_map", {})
    if not isinstance(error_map, dict):
        return []
    if not isinstance(timeout_map, dict):
        timeout_map = {}
    results: list[ErrorLocation] = []
    for file_path, entries, is_timeout in [(path, items, False) for path, items in error_map.items()] + [
        (path, items, True) for path, items in timeout_map.items()
    ]:
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", "") or "")
            status_obj = entry.get("status")
            status_text = ""
            if isinstance(status_obj, dict):
                status_text = str(status_obj.get("text", "") or "")
            elif isinstance(status_obj, str):
                status_text = status_obj
            message = f"{url} -> {status_text}" if status_text else url
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(str(file_path)),
                    line=1,
                    col=None,
                    command="lychee",
                    message=message,
                    severity="warning" if pyfltr.link_checks.is_transient_failure(entry, timeout=is_timeout) else "error",
                )
            )
    return results


def parse_semgrep_json(output: str) -> list[ErrorLocation]:
    """`semgrep scan --json`のJSON出力をパースする。

    出力例::

        {
          "results": [
            {
              "check_id": "rules.foo",
              "path": "src/foo.py",
              "start": {"line": 18, "col": 9},
              "end": {...},
              "extra": {"severity": "ERROR", "message": "..."}
            }
          ],
          ...
        }

    JSON解析失敗時は空リストを返す。
    """
    raw_results = json_list_field(output, "results")
    if raw_results is None:
        return []
    results: list[ErrorLocation] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        start = entry.get("start", {})
        if not isinstance(start, dict):
            continue
        line = start.get("line")
        if not isinstance(line, int):
            continue
        raw_col = start.get("col")
        col = raw_col if isinstance(raw_col, int) else None
        end = entry.get("end")
        end_line = json_int(end.get("line")) if isinstance(end, dict) else None
        end_col = json_int(end.get("col")) if isinstance(end, dict) else None
        extra = entry.get("extra", {}) if isinstance(entry.get("extra"), dict) else {}
        rule = str(entry.get("check_id", "") or "") or None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("path", ""))),
                line=line,
                col=col,
                command="semgrep",
                message=str(extra.get("message", "") or ""),
                rule=rule,
                severity=normalize_severity(extra.get("severity")),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_bandit_json(output: str) -> list[ErrorLocation]:
    """`bandit -f json`のJSON出力をパースする。

    出力例::

        {
          "results": [
            {
              "filename": "src/foo.py",
              "line_number": 10,
              "col_offset": 4,
              "test_id": "B101",
              "test_name": "assert_used",
              "issue_severity": "LOW",
              "issue_text": "Use of assert detected.",
              "more_info": "https://bandit.readthedocs.io/.../b101.html"
            }
          ]
        }

    JSON解析失敗時は空リストを返す。
    """
    raw_results = json_list_field(output, "results")
    if raw_results is None:
        return []
    results: list[ErrorLocation] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        line = entry.get("line_number")
        if not isinstance(line, int):
            continue
        col = one_based_offset(entry.get("col_offset"))
        line_range = entry.get("line_range")
        end_line = json_int(line_range[-1]) if isinstance(line_range, list) and line_range else None
        end_col = one_based_offset(entry.get("end_col_offset"))
        message = str(entry.get("issue_text", "") or "")
        more_info = entry.get("more_info")
        if isinstance(more_info, str) and more_info:
            message = f"{message} (see {more_info})" if message else f"(see {more_info})"
        rule = str(entry.get("test_id", "") or "") or None
        end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(str(entry.get("filename", ""))),
                line=line,
                col=col,
                command="bandit",
                message=message,
                rule=rule,
                severity=normalize_severity(entry.get("issue_severity")),
                end_line=end_line,
                end_col=end_col,
            )
        )
    return results


def parse_sqlfluff_json(output: str) -> list[ErrorLocation]:
    """`sqlfluff lint --format=json`のJSON出力をパースする。

    出力例::

        [
          {
            "filepath": "src/foo.sql",
            "violations": [
              {
                "start_line_no": 10,
                "start_line_pos": 5,
                "code": "L001",
                "name": "...",
                "description": "...",
                "warning": false
              }
            ]
          }
        ]

    JSON解析失敗時は空リストを返す。
    """
    data = try_json_loads(output)
    if not isinstance(data, list):
        return []
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("filepath", "") or "")
        violations = entry.get("violations", [])
        if not isinstance(violations, list):
            continue
        for violation in violations:
            if not isinstance(violation, dict):
                continue
            line = violation.get("start_line_no")
            if not isinstance(line, int):
                continue
            raw_col = violation.get("start_line_pos")
            col = raw_col if isinstance(raw_col, int) else None
            end_line = json_int(violation.get("end_line_no"))
            end_col = json_int(violation.get("end_line_pos"))
            rule = str(violation.get("code", "") or "") or None
            severity = "warning" if violation.get("warning") else "error"
            end_line, end_col = to_inclusive_end_position(line, end_line, end_col)
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(file_path),
                    line=line,
                    col=col,
                    command="sqlfluff",
                    message=str(violation.get("description", "") or ""),
                    rule=rule,
                    severity=severity,
                    end_line=end_line,
                    end_col=end_col,
                )
            )
    return results


_GHSA_RE = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}", re.IGNORECASE)


def extract_advisory_rule(url: str, fallback_id: typing.Any) -> str | None:
    """Advisory URLからGHSA識別子を抽出する。無ければ`fallback_id`を文字列化して返す。

    npm系auditツールはadvisory URLにGHSA識別子を含むため、機械判別可能なruleとして採用する。
    URLに含まれない場合はadvisoryの数値ID（`fallback_id`）へフォールバックし、いずれも無ければ`None`。
    """
    match = _GHSA_RE.search(url)
    if match is not None:
        return match.group(0)
    if fallback_id is not None:
        return str(fallback_id)
    return None


_UV_AUDIT_PACKAGE_RE = re.compile(r"^(?P<pkg>\S+)\s+(?P<version>\S+)\s+has\s+\d+\s+known\s+(?:vulnerability|vulnerabilities)\b")


_UV_AUDIT_ADVISORY_RE = re.compile(r"^-\s+(?P<id>\S+):\s+(?P<message>.+)$")


def parse_uv_audit(output: str) -> list[ErrorLocation]:
    """`uv audit`のテキスト出力をパースする。

    uvは機械可読出力（JSON等）の指定フラグを持たないためテキストを解析する。
    出力例（stderrの実験的警告・サマリーがpyfltr側でstdout統合され混在し得るが、脆弱性本体は次の形）::

        Vulnerabilities:

        starlette 1.0.0 has 1 known vulnerability:

        - PYSEC-2026-161: Missing Host header validation poisons request.url.path ...

          Fixed in: 1.0.1

          Advisory information: https://github.com/Kludex/starlette/security/advisories/GHSA-...

    `<pkg> <version> has N known vulnerabilities`行（単数時は vulnerability）で対象パッケージを把握し、
    続く`- <ID>: <説明>`行を1件の`ErrorLocation`へ変換する。`Fixed in:`等のインデント行は`- `で始まらないため対象外。
    uv auditは行情報を持たないため、`lychee`に倣いマニフェスト`pyproject.toml`を`file`、`line=1`固定とする。
    テキスト出力では深刻度を分類しないため`severity="error"`固定とする。
    uvはパッケージ見出し単位で各advisoryを1回ずつ列挙する（別パッケージ経由の同一IDは別診断として扱う）ため、
    JSON系3パーサーのような重複排除は行わない。
    """
    results: list[ErrorLocation] = []
    current_package = ""
    for raw_line in output.splitlines():
        line = raw_line.strip()
        package_match = _UV_AUDIT_PACKAGE_RE.match(line)
        if package_match is not None:
            current_package = f"{package_match.group('pkg')} {package_match.group('version')}"
            continue
        advisory_match = _UV_AUDIT_ADVISORY_RE.match(line)
        if advisory_match is None:
            continue
        description = advisory_match.group("message").strip()
        message = f"{current_package}: {description}" if current_package else description
        results.append(
            ErrorLocation(
                file="pyproject.toml",
                line=1,
                col=None,
                command="uv-audit",
                message=message,
                rule=advisory_match.group("id"),
                severity="error",
            )
        )
    return results


def parse_npm_audit_json(output: str) -> list[ErrorLocation]:
    """`npm audit --json`出力（auditReportVersion 2形式）をパースする。

    出力例::

        {
          "auditReportVersion": 2,
          "vulnerabilities": {
            "minimist": {
              "name": "minimist", "severity": "critical",
              "via": [
                {"source": 1097677, "title": "Prototype Pollution in minimist",
                 "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h",
                 "severity": "critical", "range": "<0.2.4"},
                "other-package"
              ]
            }
          },
          "metadata": {...}
        }

    `vulnerabilities.<pkg>.via[]`のうち辞書要素のみが実advisoryで、文字列要素は他パッケージへの
    参照のためスキップする。同一advisory（`source`一致）が複数パッケージのviaに現れ得るため重複排除する。
    行情報を持たないためマニフェスト`package.json`を`file`、`line=1`固定とする。JSON解析失敗時は空リスト。
    """
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return []
    vulnerabilities = data.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        return []
    results: list[ErrorLocation] = []
    seen: set[typing.Any] = set()
    for pkg_name, vuln in vulnerabilities.items():
        if not isinstance(vuln, dict):
            continue
        via_list = vuln.get("via", [])
        if not isinstance(via_list, list):
            continue
        for via in via_list:
            if not isinstance(via, dict):
                continue  # 文字列要素は他パッケージへの参照のためスキップ
            url = str(via.get("url", "") or "")
            title = str(via.get("title", "") or "")
            source = via.get("source")
            # 同一advisory（source一致）が複数パッケージのviaに現れ得るためsource単位で重複排除する。
            # sourceを持たない異常エントリは空キーへの衝突で誤集約しないよう重複排除対象から外し、各件出力する。
            if source is not None:
                if source in seen:
                    continue
                seen.add(source)
            name = str(via.get("name") or pkg_name)
            version_range = str(via.get("range", "") or "")
            message = f"{name}: {title}" if title else name
            if version_range:
                message = f"{message} ({version_range})"
            results.append(
                ErrorLocation(
                    file="package.json",
                    line=1,
                    col=None,
                    command="npm-audit",
                    message=message,
                    rule=extract_advisory_rule(url, source),
                    severity=normalize_audit_severity(via.get("severity")),
                    rule_url=url or None,
                )
            )
    return results


def parse_pnpm_audit_json(output: str) -> list[ErrorLocation]:
    """`pnpm audit --json`出力（advisories形式）をパースする。

    出力例::

        {
          "advisories": {
            "1097677": {
              "id": 1097677, "title": "Prototype Pollution in minimist",
              "module_name": "minimist", "severity": "critical",
              "vulnerable_versions": "<0.2.4",
              "github_advisory_id": "GHSA-xvch-5gv4-984h",
              "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h"
            }
          },
          "metadata": {...}
        }

    `advisories`はadvisory ID → advisory本体のmap。各advisoryから対象モジュール・タイトル・
    深刻度・URLを抽出する。行情報を持たないためマニフェスト`package.json`を`file`、`line=1`固定とする。
    JSON解析失敗時は空リストを返す。
    """
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return []
    advisories = data.get("advisories")
    if not isinstance(advisories, dict):
        return []
    results: list[ErrorLocation] = []
    for advisory in advisories.values():
        if not isinstance(advisory, dict):
            continue
        module = str(advisory.get("module_name", "") or "")
        title = str(advisory.get("title", "") or "")
        version_range = str(advisory.get("vulnerable_versions", "") or "")
        url = str(advisory.get("url", "") or "")
        ghsa = str(advisory.get("github_advisory_id", "") or "")
        message = f"{module}: {title}" if module else title
        if version_range:
            message = f"{message} ({version_range})"
        results.append(
            ErrorLocation(
                file="package.json",
                line=1,
                col=None,
                command="pnpm-audit",
                message=message,
                rule=ghsa or extract_advisory_rule(url, advisory.get("id")),
                severity=normalize_audit_severity(advisory.get("severity")),
                rule_url=url or None,
            )
        )
    return results


def parse_yarn_audit_jsonl(output: str) -> list[ErrorLocation]:
    """`yarn audit --json`出力（JSON Lines形式）をパースする。

    yarn classic（1.x）は1行1JSONで出力し、`type == "auditAdvisory"`の行に脆弱性情報、
    `type == "auditSummary"`の行に件数集計を持つ。`ErrorLocation`へ変換する対象は`auditAdvisory`行のみ。

    出力例（1行分）::

        {"type": "auditAdvisory", "data": {"advisory": {
          "id": 1097677, "title": "Prototype Pollution in minimist",
          "module_name": "minimist", "severity": "critical",
          "vulnerable_versions": "<0.2.4",
          "github_advisory_id": "GHSA-xvch-5gv4-984h",
          "url": "https://github.com/advisories/GHSA-xvch-5gv4-984h"}}}

    同一advisory（`id`一致）が依存経路ごとに複数行で現れ得るため重複排除する。
    行情報を持たないためマニフェスト`package.json`を`file`、`line=1`固定とする。解析できない行はスキップする。
    """
    results: list[ErrorLocation] = []
    seen: set[typing.Any] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "auditAdvisory":
            continue
        data = entry.get("data")
        advisory = data.get("advisory") if isinstance(data, dict) else None
        if not isinstance(advisory, dict):
            continue
        url = str(advisory.get("url", "") or "")
        ghsa = str(advisory.get("github_advisory_id", "") or "")
        advisory_id = advisory.get("id")
        dedup_key = advisory_id if advisory_id is not None else (ghsa or url)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        module = str(advisory.get("module_name", "") or "")
        title = str(advisory.get("title", "") or "")
        version_range = str(advisory.get("vulnerable_versions", "") or "")
        message = f"{module}: {title}" if module else title
        if version_range:
            message = f"{message} ({version_range})"
        results.append(
            ErrorLocation(
                file="package.json",
                line=1,
                col=None,
                command="yarn-audit",
                message=message,
                rule=ghsa or extract_advisory_rule(url, advisory_id),
                severity=normalize_audit_severity(advisory.get("severity")),
                rule_url=url or None,
            )
        )
    return results


def parse_glab_ci_lint(output: str) -> list[ErrorLocation]:
    """`glab ci lint`出力をパース。

    glabは行番号を出力しないため、検出した各エラーメッセージを`line=1`固定の
    `ErrorLocation`として生成する。

    無効CI出力例::

        Validating...
        .gitlab-ci.yml is invalid

        - jobs:test config contains unknown keys: foo
        - root config contains unknown keys: bar

    有効CI出力では`✓ CI/CD YAML is valid!`のみが出力されるため空リストを返す。
    """
    results: list[ErrorLocation] = []
    file_path: str | None = None
    invalid_re = re.compile(r"^\s*(?P<file>\S+)\s+is\s+invalid\b")
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = invalid_re.match(line)
        if match is not None:
            file_path = match.group("file")
            continue
        if file_path is None:
            continue
        # 番号付きエラー行（`- xxx` / `1. xxx`）のリストマーカーを除去する。
        message = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", line)
        if not message:
            continue
        results.append(
            ErrorLocation(
                file=pyfltr.paths.normalize_separators(file_path),
                line=1,
                col=None,
                command="glab-ci-lint",
                message=message,
            )
        )
    return results


def parse_vitest_json(output: str) -> list[ErrorLocation]:
    """Vitest `--reporter=json` 出力をパースする。

    入力はJest互換のJSONで、`pyfltr.command.vitest.execute_vitest`が
    `--outputFile.json=<tmpfile>` で取得したファイル内容を文字列として渡す。
    テキスト出力のregex解析より構造化情報が安定して得られる経路を採用する
    （Vitestデフォルト出力はバージョン差・カラー制御文字・テスト名のネスト表示など変動要素が多い）。

    出力構造の主要部分::

        {
          "testResults": [
            {
              "name": "/abs/path/to/foo.test.ts",
              "assertionResults": [
                {
                  "status": "failed",
                  "fullName": "Suite > nested > case",
                  "failureMessages": ["..."],
                  "location": {"line": 12, "column": 3}
                }
              ]
            }
          ]
        }

    各失敗 `assertionResult` を1件の `ErrorLocation` へ変換する。pytestのカスタムパーサーと
    同じく `fullName` を `message` 先頭へ併記する（locationだけでは識別性が乏しく、
    `expect`系のassertion失敗メッセージはテスト名と組み合わせて初めて意味を持つため）。

    `location` 欠落時は `line=1` 固定でフォールバックする
    （Vitestの新しいランナー以外では `includeTaskLocation` 設定次第で欠落するため）。
    `failureMessages` が空のときも空メッセージで1件を生成し、失敗の存在自体を保持する。

    JSON解析失敗時は空リストを返す。`command.message` フォールバック経路で従来通り
    stdout末尾が `command.message` へ格納される。
    """
    test_results = json_list_field(output, "testResults")
    if test_results is None:
        return []
    results: list[ErrorLocation] = []
    for entry in test_results:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("name", "") or "")
        assertions = entry.get("assertionResults", [])
        if not isinstance(assertions, list):
            continue
        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if assertion.get("status") != "failed":
                continue
            location = assertion.get("location")
            line: int = 1
            col: int | None = None
            if isinstance(location, dict):
                raw_line = location.get("line")
                if isinstance(raw_line, int):
                    line = raw_line
                raw_col = location.get("column")
                if isinstance(raw_col, int):
                    col = raw_col
            test_name = str(assertion.get("fullName", "") or "")
            failure_messages = assertion.get("failureMessages", [])
            raw_message = ""
            if isinstance(failure_messages, list) and failure_messages:
                first = failure_messages[0]
                if isinstance(first, str):
                    raw_message = first.splitlines()[0] if first else ""
            message = f"{test_name}: {raw_message}" if test_name else raw_message
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.normalize_separators(file_path),
                    line=line,
                    col=col,
                    command="vitest",
                    message=message,
                )
            )
    return results


def summarize_pyright_json(output: str) -> str | None:
    """Pyright --outputjson出力からsummaryフィールドを抽出する。"""
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return None
    files_analyzed = summary.get("filesAnalyzed")
    error_count = summary.get("errorCount", 0)
    warning_count = summary.get("warningCount", 0)
    if not isinstance(files_analyzed, int):
        return None
    return f"{files_analyzed} files analyzed, {error_count} errors, {warning_count} warnings"


def summarize_pylint_json(output: str) -> str | None:
    """Pylint --output-format=json2出力からstatisticsフィールドを抽出する。"""
    data = try_json_loads(output)
    if not isinstance(data, dict):
        return None
    statistics = data.get("statistics")
    if not isinstance(statistics, dict):
        return None
    modules = statistics.get("modulesLinted")
    score = statistics.get("score")
    if not isinstance(modules, int):
        return None
    if isinstance(score, int | float):
        return f"{modules} modules linted, score: {score:.1f}"
    return f"{modules} modules linted"
