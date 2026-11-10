"""Pythonのlinterの出力解析。"""

import typing

import pyfltr.paths
import pyfltr.rule_urls
import pyfltr.tools
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import (
    json_int,
    json_list_field,
    normalize_severity,
    one_based_offset,
    parse_with_pattern,
    to_inclusive_end_position,
    try_json_loads,
)


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
