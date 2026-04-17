"""エラー出力パーサー。

各コマンドの出力からエラー箇所(ファイル名:行番号)を抽出する。
ビルトインパーサーとカスタム正規表現の両方に対応。
"""

import contextlib
import dataclasses
import json
import pathlib
import re
import typing

import pyfltr.rule_urls


@dataclasses.dataclass
class ErrorLocation:
    """エラー箇所の情報。"""

    file: str
    line: int
    col: int | None
    command: str
    message: str
    rule: str | None = None
    """ルールコード (F401, C0114, SC2086等)"""
    severity: str | None = None
    """診断の重要度 ("error" | "warning" | "info")"""
    fix: str | None = None
    """自動修正の適用可能性 ("safe" | "unsafe" | "suggested")"""
    rule_url: str | None = None
    """ルールドキュメントの URL (None は未対応ツールまたは rule 未設定時)"""


def parse_errors(command: str, output: str, error_pattern: str | None = None) -> list[ErrorLocation]:
    """コマンド出力からエラー箇所をパースする。

    優先順位:
        1. error_pattern (カスタム正規表現) が指定されていればそれを使用
        2. コマンド専用の関数ベースパーサー (JSON 出力など regex で扱いにくいもの)
        3. ビルトイン正規表現パーサー
        4. いずれも無ければ空リスト
    """
    if error_pattern is not None:
        return _parse_with_pattern(command, output, error_pattern)
    custom_parser = _CUSTOM_PARSERS.get(command)
    if custom_parser is not None:
        return custom_parser(output)
    builtin = _BUILTIN_PATTERNS.get(command)
    if builtin is not None:
        return _parse_with_pattern(command, output, builtin)
    return []


def sort_errors(errors: list[ErrorLocation], command_names: list[str]) -> list[ErrorLocation]:
    """エラー箇所をファイル:行番号でソートし、同一箇所はcommand_names順に並べる。"""

    def sort_key(e: ErrorLocation) -> tuple[str, int, int, int]:
        cmd_index = command_names.index(e.command) if e.command in command_names else len(command_names)
        return (e.file, e.line, e.col or 0, cmd_index)

    return sorted(errors, key=sort_key)


def get_custom_parser_commands() -> set[str]:
    """カスタムパーサーが登録されているコマンド名の集合を返す。"""
    return set(_CUSTOM_PARSERS.keys())


def format_error(error: ErrorLocation) -> str:
    """エラー箇所を表示用文字列にフォーマットする。"""
    col_str = f":{error.col}" if error.col else ""
    tag = f"{error.command}:{error.rule}" if error.rule else error.command
    return f"{error.file}:{error.line}{col_str}: [{tag}] {error.message}"


def parse_summary(command: str, output: str) -> str | None:
    """コマンド出力からサマリー文字列を抽出する。

    カスタムサマリーパーサーがあればそれを使い、なければテキスト出力の
    末尾行をフォールバックで抽出する。JSON出力はフォールバック対象外。
    """
    parser = _SUMMARY_PARSERS.get(command)
    if parser is not None:
        return parser(output)
    return _extract_last_line(output)


# ビルトインパーサー用の正規表現パターン
# 各パターンはfile, line, messageの名前付きグループが必須。colは任意。
# rule グループが存在する場合は ErrorLocation.rule に取り込まれる (_parse_with_pattern で対応)。
# ファイルパスのパターンは (?:[A-Za-z]:)? でWindowsドライブレターに対応する。
_FILE = r"(?:[A-Za-z]:)?[^\s:]+"
_BUILTIN_PATTERNS: dict[str, str] = {
    # mypy出力例: src/foo.py:10: error: xxx [error-code]
    # 末尾の [error-code] を rule グループとして抽出する。
    "mypy": rf"(?P<file>{_FILE}):(?P<line>\d+):\s*error:\s*(?P<message>.+?)(?:\s*\[(?P<rule>[^\]]+)\])?\s*$",
    # pylint出力例: src/foo.py:10:5: C0114: xxx
    "pylint": rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[CRWEF]\d+:.+)",
    # ruff check出力例: src/foo.py:10:5: E001 xxx
    "ruff-check": rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[A-Z]+\d+\s+.+)",
    # pyright出力例: src/foo.py:10:5 - error: xxx
    "pyright": rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+)\s*-\s*error:\s*(?P<message>.+)",
    # ty check --output-format concise 出力例: src/foo.py:10:5: error[rule-name] Message text
    "ty": rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>(?:error|warning)\[.+?\]\s+.+)",
    # markdownlint-cli2出力例: file.md:3 MD001/heading-increment Heading levels ...
    # 先頭の MDxxx を rule グループとして抽出する (スラッシュ以降のシンボルは message に残す)。
    "markdownlint": rf"(?P<file>{_FILE}):(?P<line>\d+)\s+(?P<rule>MD\d+)(?P<message>\S*\s+.+)",
    # textlint --format compact出力例: /path/file.md: line 1, col 1, Error - message (rule)
    "textlint": rf"(?P<file>{_FILE}):\s*line\s+(?P<line>\d+),\s*col\s+(?P<col>\d+),\s*\w+\s*-\s*(?P<message>.+)",
    # pytest出力例: FAILED tests/xxx_test.py::test_yyy - AssertionError
    "pytest": rf"FAILED\s+(?P<file>{_FILE})::(?P<message>\S+)",
    # biome --reporter=github 出力例 (実機確認済み、line と col の間に endLine が挟まる):
    # ::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::Use === instead of ==
    # [^:]*? で順序非依存かつ `::` 終端を跨がないようマッチする。
    "biome": (
        r"::(?:error|warning)\s+[^:]*?file=(?P<file>[^,]+)"
        r"[^:]*?line=(?P<line>\d+)"
        r"[^:]*?col=(?P<col>\d+)"
        r"[^:]*?::(?P<message>.+)"
    ),
    # ec (editorconfig-checker) -format gcc 出力例: src/foo.py:10:0: error: xxx
    "ec": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*\w+:\s*(?P<message>.+)",
    # shellcheck -f gcc 出力例: src/foo.sh:10:5: warning: xxx [SC2086]
    "shellcheck": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*\w+:\s*(?P<message>.+)",
    # typos --format brief 出力例: src/foo.py:10:5: `typo` -> `correction`
    "typos": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
    # actionlint 出力例: .github/workflows/ci.yaml:10:5: xxx [rule-name]
    "actionlint": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
}


def _try_json_loads(output: str) -> typing.Any:
    """JSON パースを試みる。失敗時は None を返す。"""
    output = output.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return None


def _normalize_severity(value: typing.Any) -> str | None:
    """生の severity 値を `"error" / "warning" / "info"` の 3 値に正規化する。

    未知の値や None は ``None`` を返し、JSONL 出力側で省略される。
    """
    if value is None:
        return None
    if isinstance(value, int):
        return _eslint_severity(value)
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if not lowered:
        return None
    if lowered in ("error", "fatal"):
        return "error"
    if lowered in ("warning", "warn"):
        return "warning"
    if lowered in ("info", "information", "informational", "note", "hint", "style", "convention", "refactor"):
        return "info"
    return None


def _eslint_severity(value: typing.Any) -> str | None:
    """ESLint/textlint の severity 数値を文字列に変換する。"""
    if value == 2:
        return "error"
    if value == 1:
        return "warning"
    return None


def _parse_eslint_json(output: str) -> list[ErrorLocation]:
    """ESLint --format json 出力をパース。

    ESLint 9 系以降で compact / unix などのコアフォーマッタが除去されたため、
    pyfltr では `--format json` を使う。出力は以下のような配列。

    [
      {
        "filePath": "/abs/src/foo.js",
        "messages": [
          {"line": 10, "column": 5, "message": "...", "ruleId": "no-unused-vars", "severity": 2}
        ]
      }
    ]

    stderr 混入等でパースに失敗した場合は空リストを返す (regex パーサーが
    マッチしない時の挙動と揃える)。
    """
    data = _try_json_loads(output)
    if not isinstance(data, list):
        return []
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("filePath", ""))
        messages = entry.get("messages", [])
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            line = msg.get("line")
            if not isinstance(line, int):
                continue
            raw_col = msg.get("column")
            col = raw_col if isinstance(raw_col, int) else None
            rule_id = str(msg.get("ruleId") or "")
            text = str(msg.get("message", ""))
            message = f"{text} ({rule_id})" if rule_id else text
            fix_value = "safe" if msg.get("fix") else None
            rule = rule_id or None
            results.append(
                ErrorLocation(
                    file=_normalize_path(file_path),
                    line=line,
                    col=col,
                    command="eslint",
                    message=message.strip(),
                    rule=rule,
                    severity=_normalize_severity(msg.get("severity")),
                    fix=fix_value,
                    rule_url=pyfltr.rule_urls.build_rule_url("eslint", rule),
                )
            )
    return results


def _parse_ruff_check_json(output: str) -> list[ErrorLocation]:
    """Ruff check --output-format=json 出力をパース。JSON 解析失敗時は regex にフォールバック。"""
    data = _try_json_loads(output)
    if not isinstance(data, list):
        return _parse_with_pattern("ruff-check", output, _BUILTIN_PATTERNS["ruff-check"])
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
        fix_obj = entry.get("fix")
        fix_value: str | None = None
        if isinstance(fix_obj, dict):
            fix_value = str(fix_obj.get("applicability", "safe"))
        rule = str(entry.get("code", "")) or None
        entry_url = entry.get("url")
        existing_url = str(entry_url) if isinstance(entry_url, str) and entry_url else None
        results.append(
            ErrorLocation(
                file=_normalize_path(str(entry.get("filename", ""))),
                line=line,
                col=col,
                command="ruff-check",
                message=str(entry.get("message", "")),
                rule=rule,
                severity=_normalize_severity(entry.get("severity")) or "error",
                fix=fix_value,
                rule_url=pyfltr.rule_urls.build_rule_url("ruff-check", rule, existing_url=existing_url),
            )
        )
    return results


def _parse_pylint_json(output: str) -> list[ErrorLocation]:
    """Pylint --output-format=json2 出力をパース。JSON 解析失敗時は regex にフォールバック。

    公式ドキュメント URL が ``symbol`` 基準 (`missing-module-docstring` 等) のため、
    ``ErrorLocation.rule`` には ``symbol`` を格納する。``messageId`` (`C0114` 等) は
    ``ErrorLocation.message`` の先頭に付与して保持する。
    """
    data = _try_json_loads(output)
    if not isinstance(data, dict) or "messages" not in data:
        return _parse_with_pattern("pylint", output, _BUILTIN_PATTERNS["pylint"])
    messages = data.get("messages", [])
    if not isinstance(messages, list):
        return _parse_with_pattern("pylint", output, _BUILTIN_PATTERNS["pylint"])
    results: list[ErrorLocation] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        line = msg.get("line")
        if not isinstance(line, int):
            continue
        raw_col = msg.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        msg_type = str(msg.get("type", "")).lower()
        severity = "error" if msg_type in ("error", "fatal") else "warning"
        symbol = str(msg.get("symbol") or "") or None
        message_id = str(msg.get("messageId") or "")
        original_message = str(msg.get("message", ""))
        # 既存 rule スキーマ (機械判別可能な識別子) と messageId の両方を JSONL 上に残す。
        combined_message = f"{message_id}: {original_message}" if message_id else original_message
        # 公式ドキュメント URL はカテゴリー名 (`convention` / `warning` / `error` / `refactor` /
        # `information` / `fatal`) を必要とする。``type`` フィールドをそのまま渡す。
        category = msg_type or None
        results.append(
            ErrorLocation(
                file=_normalize_path(str(msg.get("path", ""))),
                line=line,
                col=col,
                command="pylint",
                message=combined_message,
                rule=symbol,
                severity=severity,
                rule_url=pyfltr.rule_urls.build_rule_url("pylint", symbol, category=category),
            )
        )
    return results


def _parse_pyright_json(output: str) -> list[ErrorLocation]:
    """Pyright --outputjson 出力をパース。JSON 解析失敗時は regex にフォールバック。"""
    data = _try_json_loads(output)
    if not isinstance(data, dict) or "generalDiagnostics" not in data:
        return _parse_with_pattern("pyright", output, _BUILTIN_PATTERNS["pyright"])
    diags = data.get("generalDiagnostics", [])
    if not isinstance(diags, list):
        return _parse_with_pattern("pyright", output, _BUILTIN_PATTERNS["pyright"])
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
        # pyright の line/character は 0-based
        line = start.get("line")
        if not isinstance(line, int):
            continue
        raw_char = start.get("character")
        col = (raw_char + 1) if isinstance(raw_char, int) else None
        rule = str(diag.get("rule", "")) or None
        results.append(
            ErrorLocation(
                file=_normalize_path(str(diag.get("file", ""))),
                line=line + 1,
                col=col,
                command="pyright",
                message=str(diag.get("message", "")),
                rule=rule,
                severity=_normalize_severity(diag.get("severity")),
                rule_url=pyfltr.rule_urls.build_rule_url("pyright", rule),
            )
        )
    return results


def _parse_shellcheck_json(output: str) -> list[ErrorLocation]:
    """Shellcheck -f json 出力をパース。JSON 解析失敗時は regex にフォールバック。"""
    data = _try_json_loads(output)
    if not isinstance(data, list):
        return _parse_with_pattern("shellcheck", output, _BUILTIN_PATTERNS["shellcheck"])
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        line = entry.get("line")
        if not isinstance(line, int):
            continue
        raw_col = entry.get("column")
        col = raw_col if isinstance(raw_col, int) else None
        code = entry.get("code")
        rule = f"SC{code}" if isinstance(code, int) else None
        fix_value = "safe" if entry.get("fix") else None
        results.append(
            ErrorLocation(
                file=_normalize_path(str(entry.get("file", ""))),
                line=line,
                col=col,
                command="shellcheck",
                message=str(entry.get("message", "")),
                rule=rule,
                severity=_normalize_severity(entry.get("level")),
                fix=fix_value,
                rule_url=pyfltr.rule_urls.build_rule_url("shellcheck", rule),
            )
        )
    return results


def _parse_textlint_json(output: str) -> list[ErrorLocation]:
    """Textlint --format json 出力をパース。JSON 解析失敗時は regex にフォールバック。

    出力構造は ESLint と同じ filePath + messages 配列形式。
    """
    data = _try_json_loads(output)
    if not isinstance(data, list):
        return _parse_with_pattern("textlint", output, _BUILTIN_PATTERNS["textlint"])
    results: list[ErrorLocation] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        file_path = str(entry.get("filePath", ""))
        messages = entry.get("messages", [])
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            line = msg.get("line")
            if not isinstance(line, int):
                continue
            raw_col = msg.get("column")
            col = raw_col if isinstance(raw_col, int) else None
            rule_id = str(msg.get("ruleId") or "")
            fix_value = "safe" if msg.get("fix") else None
            results.append(
                ErrorLocation(
                    file=_normalize_path(file_path),
                    line=line,
                    col=col,
                    command="textlint",
                    message=str(msg.get("message", "")).strip(),
                    rule=rule_id or None,
                    severity=_normalize_severity(msg.get("severity")),
                    fix=fix_value,
                )
            )
    return results


def _parse_typos_jsonl(output: str) -> list[ErrorLocation]:
    """Typos --format=json 出力をパース（JSON Lines 形式）。解析失敗時は regex にフォールバック。"""
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
        # typos の JSON エントリには type フィールドがある。typo 以外（binary等）はスキップ
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
            message = f"`{typo}`"
            fix_value = None
        results.append(
            ErrorLocation(
                file=_normalize_path(str(entry.get("path", ""))),
                line=line_num,
                col=None,
                command="typos",
                message=message,
                severity="warning",
                fix=fix_value,
            )
        )
    if not any_parsed and output.strip():
        return _parse_with_pattern("typos", output, _BUILTIN_PATTERNS["typos"])
    return results


def _parse_pytest(output: str) -> list[ErrorLocation]:
    """Pytest出力をパース。--tb=short形式のトレースバックからプロジェクト内フレームを優先的に抽出する。"""
    failures_start = output.find("= FAILURES =")
    summary_start = output.find("short test summary info")
    if failures_start < 0:
        return _parse_with_pattern("pytest", output, _BUILTIN_PATTERNS["pytest"])

    end = summary_start if summary_start > failures_start else len(output)
    failures_section = output[failures_start:end]

    # テスト単位のブロックに分割（`_ test_name _` 区切り）
    block_re = re.compile(r"^_+ .+ _+$", re.MULTILINE)
    block_starts = [m.end() for m in block_re.finditer(failures_section)]
    if not block_starts:
        return _parse_with_pattern("pytest", output, _BUILTIN_PATTERNS["pytest"])

    # フレーム行: file:line: in func_name
    frame_re = re.compile(rf"^(?P<file>{_FILE}):(?P<line>\d+): in .+$", re.MULTILINE)
    # エラー行: E   message
    error_re = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)

    results: list[ErrorLocation] = []
    for i, start in enumerate(block_starts):
        block_end = block_starts[i + 1] if i + 1 < len(block_starts) else len(failures_section)
        block = failures_section[start:block_end]

        # フレーム群から最後のプロジェクト内フレームを選択
        frames = list(frame_re.finditer(block))
        if not frames:
            continue

        chosen = frames[-1]  # フォールバック: 最後のフレーム
        for frame in reversed(frames):
            if _is_project_path(_normalize_path(frame.group("file"))):
                chosen = frame
                break

        # エラーメッセージ（先頭のE行）
        error_match = error_re.search(block)
        message = error_match.group("message").strip() if error_match else ""

        results.append(
            ErrorLocation(
                file=_normalize_path(chosen.group("file")),
                line=int(chosen.group("line")),
                col=None,
                command="pytest",
                message=message,
            )
        )

    if results:
        return results
    # フォールバック: FAILED file::test_name パターン（line=0）
    return _parse_with_pattern("pytest", output, _BUILTIN_PATTERNS["pytest"])


# コマンド名 -> 関数ベースパーサー。regex で扱いにくい出力 (JSON など) に使う。
_CUSTOM_PARSERS: dict[str, typing.Callable[[str], list[ErrorLocation]]] = {
    "eslint": _parse_eslint_json,
    "ruff-check": _parse_ruff_check_json,
    "pylint": _parse_pylint_json,
    "pyright": _parse_pyright_json,
    "shellcheck": _parse_shellcheck_json,
    "textlint": _parse_textlint_json,
    "typos": _parse_typos_jsonl,
    "pytest": _parse_pytest,
}


def _summarize_pyright_json(output: str) -> str | None:
    """Pyright --outputjson 出力から summary フィールドを抽出する。"""
    data = _try_json_loads(output)
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


def _summarize_pylint_json(output: str) -> str | None:
    """Pylint --output-format=json2 出力から statistics フィールドを抽出する。"""
    data = _try_json_loads(output)
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


def _summarize_pytest(output: str) -> str | None:
    """Pytest 出力末尾のサマリー行から = パディングを除去して抽出する。"""
    match = re.search(r"=+ (.+?) =+\s*$", output)
    if match is None:
        return None
    return match.group(1)


# コマンド名 -> サマリーパーサー。JSON 出力にサマリーフィールドを持つツールや、
# テキスト出力の整形が必要なツール向け。未登録のテキスト出力ツールは
# _extract_last_line() でフォールバックする。
_SUMMARY_PARSERS: dict[str, typing.Callable[[str], str | None]] = {
    "pyright": _summarize_pyright_json,
    "pylint": _summarize_pylint_json,
    "pytest": _summarize_pytest,
}


def _parse_with_pattern(command: str, output: str, pattern: str) -> list[ErrorLocation]:
    """正規表現パターンでエラー箇所をパースする。

    パターンに名前付きグループ ``rule`` が含まれる場合、マッチ内容を
    ``ErrorLocation.rule`` に格納し、``rule_urls.build_rule_url()`` で URL も補完する。
    """
    compiled = re.compile(pattern)
    results: list[ErrorLocation] = []
    for line in output.splitlines():
        match = compiled.search(line)
        if match is None:
            continue
        groups = match.groupdict()
        file_path = groups.get("file", "")
        line_str = groups.get("line", "0")
        col_str = groups.get("col")
        message = groups.get("message") or ""
        try:
            line_num = int(line_str)
        except ValueError:
            continue
        col_num: int | None = None
        if col_str is not None:
            with contextlib.suppress(ValueError):
                col_num = int(col_str)
        rule_raw = groups.get("rule")
        rule = rule_raw.strip() if isinstance(rule_raw, str) and rule_raw.strip() else None
        rule_url = pyfltr.rule_urls.build_rule_url(command, rule) if rule is not None else None
        results.append(
            ErrorLocation(
                file=_normalize_path(file_path),
                line=line_num,
                col=col_num,
                command=command,
                message=message.strip(),
                rule=rule,
                rule_url=rule_url,
            )
        )
    return results


def _extract_last_line(output: str) -> str | None:
    """テキスト出力の末尾から意味のある行を抽出する。

    JSON出力（先頭が [ または {）は対象外。区切り線のみの行はスキップする。
    """
    stripped = output.strip()
    if not stripped or stripped[0] in ("[", "{"):
        return None
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line and not re.fullmatch(r"[=\-*#]+", line):
            return line
    return None


def _normalize_path(file_path: str) -> str:
    """パスをcwd基準の相対パスに正規化する。区切り文字はスラッシュに統一する。"""
    path = pathlib.Path(file_path)
    if path.is_absolute():
        try:
            result = str(path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            # cwdの配下でない場合はそのまま返す
            return file_path
        return result.replace("\\", "/")
    return file_path.replace("\\", "/")


def _is_project_path(normalized_path: str) -> bool:
    """正規化済みパスがプロジェクト内のファイルかを判定する。

    以下を全て満たす場合にプロジェクト内と見なす:
    - 相対パスである（絶対パスはcwd外 = 標準ライブラリ等）
    - ``..``で始まらない（uv管理Pythonの標準ライブラリ等）
    - ``.venv/``で始まらない（仮想環境内サードパーティー）
    - ``site-packages/``・``dist-packages/``を含まない（名前の異なる仮想環境内サードパーティー）
    """
    if pathlib.PurePosixPath(normalized_path).is_absolute():
        return False
    if normalized_path.startswith(".."):
        return False
    if normalized_path.startswith(".venv/"):
        return False
    return not ("site-packages/" in normalized_path or "dist-packages/" in normalized_path)
