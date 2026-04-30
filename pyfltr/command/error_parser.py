"""エラー出力パーサー。

各コマンドの出力からエラー箇所（ファイル名:行番号）を抽出する。
ビルトインパーサーとカスタム正規表現の両方に対応。
"""

import contextlib
import dataclasses
import json
import pathlib
import re
import typing

import pyfltr.output.rule_urls
import pyfltr.paths


@dataclasses.dataclass
class ErrorLocation:
    """エラー箇所の情報。"""

    file: str
    line: int
    col: int | None
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
    """違反範囲の終端行（Noneはツールが範囲を返さない場合）。

    現状はtextlint v12+の`loc.end.line`のみが詰める。pyright・biome等にも将来拡張可。
    """
    end_col: int | None = None
    """違反範囲の終端列（Noneはツールが範囲を返さない場合）。

    textlintの`column`系はノード先頭からの累積位置を返す仕様のため、本フィールドも
    同様の系で出力する。行内オフセットへの正規化はファイル本文の参照を要するため行わない。
    """


_TEXTLINT_RULE_HINTS: dict[str, str] = {
    "ja-technical-writing/sentence-length": (
        "textlint counts up to the period (。) as one sentence; bullet-line splits still count as one."
        " Split with periods to shorten."
    ),
    "ja-technical-writing/max-ten": (
        "Too many commas (、) in one sentence; split into multiple sentences or revise conjunctions and dependencies."
    ),
    "ja-technical-writing/max-kanji-continuous-len": (
        "Long kanji run detected; insert hiragana, particles, or commas (、) to break it up."
    ),
}
"""textlintの頻出ルール向けヒント辞書。利用者が踏みやすいルールに限定している。

ヒント文字列はルール固有の修正観点のみに留める（重複させず、3ルール中1ルールのみが
膨らむのを避けるため）。各`ErrorLocation`の`hint`フィールドに詰められ、
`aggregate_diagnostics()`によってcommandレコードの`command.hints`辞書へ集約される。
"""


def parse_errors(command: str, output: str, error_pattern: str | None = None) -> list[ErrorLocation]:
    """コマンド出力からエラー箇所をパースする。

    優先順位:
        1. error_pattern（カスタム正規表現）が指定されていればそれを使用
        2. コマンド専用の関数ベースパーサー（JSON出力などregexで扱いにくいもの）
        3. ビルトイン正規表現パーサー
        4. いずれもなければ空リスト
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
    """エラー箇所を`file:line[:col]: [tool[:rule]] message`のテキスト形式にフォーマットする。"""
    col_str = f":{error.col}" if error.col else ""
    tag = f"{error.command}:{error.rule}" if error.rule else error.command
    return f"{error.file}:{error.line}{col_str}: [{tag}] {error.message}"


def format_error_github(error: ErrorLocation) -> str:
    """エラー箇所をGitHub Actionsのワークフローコマンド記法にフォーマットする。

    `::error file=...::message`形式で出力する。
    """
    from pyfltr.output import github_annotations  # pylint: disable=import-outside-toplevel

    return github_annotations.build_workflow_command(error)


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
# ruleグループが存在する場合はErrorLocation.ruleに取り込まれる（_parse_with_patternで対応）。
# ファイルパスのパターンは(?:[A-Za-z]:)?でWindowsドライブレターに対応する。
_FILE = r"(?:[A-Za-z]:)?[^\s:]+"
_BUILTIN_PATTERNS: dict[str, str] = {
    # mypy出力例: src/foo.py:10: error: xxx [error-code]
    # 末尾の[error-code]をruleグループとして抽出する。
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
    # 先頭のMDxxxをruleグループとして抽出する（スラッシュ以降のシンボルはmessageに残す）。
    "markdownlint": rf"(?P<file>{_FILE}):(?P<line>\d+)\s+(?P<rule>MD\d+)(?P<message>\S*\s+.+)",
    # textlint --format compact出力例: /path/file.md: line 1, col 1, Error - message (rule)
    "textlint": rf"(?P<file>{_FILE}):\s*line\s+(?P<line>\d+),\s*col\s+(?P<col>\d+),\s*\w+\s*-\s*(?P<message>.+)",
    # pytest出力例: FAILED tests/xxx_test.py::test_yyy - AssertionError
    "pytest": rf"FAILED\s+(?P<file>{_FILE})::(?P<message>\S+)",
    # biome --reporter=github出力例（実機確認済み、lineとcolの間にendLineが挟まる）:
    # ::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::Use === instead of ==
    # [^:]*?で順序非依存かつ`::`終端を跨がないようマッチする。
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
    """JSONパースを試みる。失敗時はNoneを返す。

    一部ツール（例: pylint）は`PYTHONDEVMODE=1`環境で読み込んだプラグインの
    `DeprecationWarning`などをJSON本体の前にテキストで流し込む。そのままでは
    パースが必ず失敗するため、先頭の`{`または`[`を見つけて、それ以前の
    ゴミ文字列を落としてから再試行するフォールバックを行う。
    """
    stripped = output.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    # 先頭がJSON以外の行で汚染されているケースを救済する。
    for start_char in ("{", "["):
        index = stripped.find(start_char)
        if index > 0:
            try:
                return json.loads(stripped[index:])
            except json.JSONDecodeError:
                continue
    return None


def _normalize_severity(value: typing.Any) -> str | None:
    """生のseverity値を`"error" / "warning" / "info"`の3値に正規化する。

    未知の値やNoneは`None`を返し、JSONL出力側で省略される。
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
            # ESLintのJSONはautofixがある場合のみ`fix`オブジェクトが付与される。
            # 自動修正情報の有無を報告するツールなので、欠落時は`"none"`として
            # 「自動修正不可」を明示する（`None`省略との区別を維持）。
            fix_value = "safe" if msg.get("fix") else "none"
            rule = rule_id or None
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.to_cwd_relative(file_path),
                    line=line,
                    col=col,
                    command="eslint",
                    message=message.strip(),
                    rule=rule,
                    severity=_normalize_severity(msg.get("severity")),
                    fix=fix_value,
                    rule_url=pyfltr.output.rule_urls.build_rule_url("eslint", rule),
                )
            )
    return results


def _parse_ruff_check_json(output: str) -> list[ErrorLocation]:
    """Ruff check --output-format=json出力をパース。JSON解析失敗時はregexにフォールバック。"""
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
        # ruffは自動修正情報の有無を明示的に返すツール。`fix`欠落時は
        # 自動修正不可として`"none"`を出力する。
        fix_value: str | None = str(fix_obj.get("applicability", "safe")) if isinstance(fix_obj, dict) else "none"
        rule = str(entry.get("code", "")) or None
        entry_url = entry.get("url")
        existing_url = str(entry_url) if isinstance(entry_url, str) and entry_url else None
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(str(entry.get("filename", ""))),
                line=line,
                col=col,
                command="ruff-check",
                message=str(entry.get("message", "")),
                rule=rule,
                severity=_normalize_severity(entry.get("severity")) or "error",
                fix=fix_value,
                rule_url=pyfltr.output.rule_urls.build_rule_url("ruff-check", rule, existing_url=existing_url),
            )
        )
    return results


def _parse_pylint_json(output: str) -> list[ErrorLocation]:
    """Pylint --output-format=json2出力をパース。JSON解析失敗時はregexにフォールバック。

    公式ドキュメントURLが`symbol`基準（`missing-module-docstring`等）のため、
    `ErrorLocation.rule`には`symbol`を格納する。`messageId`（`C0114`等）は
    `ErrorLocation.message`の先頭に付与して保持する。
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
        # 既存ruleスキーマ（機械判別可能な識別子）とmessageIdの両方をJSONL上に残す。
        combined_message = f"{message_id}: {original_message}" if message_id else original_message
        # 公式ドキュメントURLはカテゴリー名（`convention` / `warning` / `error` / `refactor` /
        # `information` / `fatal`）を必要とする。`type`フィールドをそのまま渡す。
        category = msg_type or None
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(str(msg.get("path", ""))),
                line=line,
                col=col,
                command="pylint",
                message=combined_message,
                rule=symbol,
                severity=severity,
                rule_url=pyfltr.output.rule_urls.build_rule_url("pylint", symbol, category=category),
            )
        )
    return results


def _parse_pyright_json(output: str) -> list[ErrorLocation]:
    """Pyright --outputjson出力をパース。JSON解析失敗時はregexにフォールバック。"""
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
        # pyrightのline/characterは0-based
        line = start.get("line")
        if not isinstance(line, int):
            continue
        raw_char = start.get("character")
        col = (raw_char + 1) if isinstance(raw_char, int) else None
        rule = str(diag.get("rule", "")) or None
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(str(diag.get("file", ""))),
                line=line + 1,
                col=col,
                command="pyright",
                message=str(diag.get("message", "")),
                rule=rule,
                severity=_normalize_severity(diag.get("severity")),
                rule_url=pyfltr.output.rule_urls.build_rule_url("pyright", rule),
            )
        )
    return results


def _parse_shellcheck_json(output: str) -> list[ErrorLocation]:
    """Shellcheck -f json出力をパース。JSON解析失敗時はregexにフォールバック。"""
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
        # shellcheckはJSON出力で自動修正情報の有無を明示する。
        fix_value = "safe" if entry.get("fix") else "none"
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(str(entry.get("file", ""))),
                line=line,
                col=col,
                command="shellcheck",
                message=str(entry.get("message", "")),
                rule=rule,
                severity=_normalize_severity(entry.get("level")),
                fix=fix_value,
                rule_url=pyfltr.output.rule_urls.build_rule_url("shellcheck", rule),
            )
        )
    return results


def _parse_textlint_json(output: str) -> list[ErrorLocation]:
    """Textlint --format json出力をパース。JSON解析失敗時はregexにフォールバック。

    出力構造はESLintと同じfilePath + messages配列形式。
    textlintはルールによって複数行にわたるmessage（sentence-lengthの`Over X characters.`等）を返すため、
    JSONL `messages[].msg`は1行に保つ目的で改行を半角スペースへ畳む。
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
            # textlintはJSON出力でautofixの有無を明示する。
            fix_value = "safe" if msg.get("fix") else "none"
            rule = rule_id or None
            hint = _TEXTLINT_RULE_HINTS.get(rule_id) if rule_id else None
            # textlint側のmsgは複数行になり得るため、JSONL `messages[].msg`では空白へ畳む。
            # 範囲表記`(L17:1〜23)`を末尾へ視認しやすく追加する都合上、先に1行化しておく必要がある。
            message = _normalize_whitespace(str(msg.get("message", "")))
            end_line, end_col = _extract_textlint_end_position(msg.get("loc"))
            # sentence-length違反では文の起点・終点が分からないと修正しづらいため、
            # textlint v12+が返す`loc`フィールドから範囲表記を組み立てて末尾に併記する。
            # 他ルールでは違反箇所自体が短く、併記が冗長になるため対象外。
            if rule_id == "ja-technical-writing/sentence-length":
                range_text = _format_textlint_loc(msg.get("loc"))
                if range_text:
                    message = f"{message} {range_text}"
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.to_cwd_relative(file_path),
                    line=line,
                    col=col,
                    command="textlint",
                    message=message,
                    rule=rule,
                    severity=_normalize_severity(msg.get("severity")),
                    fix=fix_value,
                    hint=hint,
                    end_line=end_line,
                    end_col=end_col,
                )
            )
    return results


def _extract_textlint_loc_positions(
    loc: typing.Any,
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """Textlintの`loc`から`start`/`end`の`(line, col)`ペアを独立に取り出す。

    片方のみが有効な`loc`にも対応するため、`start`と`end`は独立に検証する
    （古いtextlintや一部ルールが`end`のみを返すケースでも有効値を失わないため）。
    `loc`不在・形式不一致は`(None, None)`を返す。
    """
    if not isinstance(loc, dict):
        return None, None
    return _extract_textlint_point(loc.get("start")), _extract_textlint_point(loc.get("end"))


def _extract_textlint_point(point: typing.Any) -> tuple[int, int] | None:
    """Textlintの`{"line": int, "column": int}`形式から`(line, col)`を取り出す。"""
    if not isinstance(point, dict):
        return None
    line = point.get("line")
    col = point.get("column")
    if not isinstance(line, int) or not isinstance(col, int):
        return None
    return line, col


def _extract_textlint_end_position(loc: typing.Any) -> tuple[int | None, int | None]:
    """Textlintの`loc.end`から`(end_line, end_col)`を取り出す。

    `loc`不在・形式不一致は`(None, None)`を返す（古いtextlintへの後方互換）。
    取り出した`end_line`/`end_col`はErrorLocationにそのまま詰める。
    """
    _, end = _extract_textlint_loc_positions(loc)
    if end is None:
        return None, None
    return end


def _format_textlint_loc(loc: typing.Any) -> str:
    """Textlintの`loc`フィールドから`(L17:1〜23)`形式の範囲文字列を組み立てる。

    1行内で完結する場合は`(Lstart:start_col〜end_col)`、
    複数行にまたがる場合は`(Lstart:start_col〜Lend:end_col)`を返す。
    `start`/`end`のいずれかが欠けている場合は空文字列を返す（古いtextlintや未提供ルールへの後方互換）。
    """
    start, end = _extract_textlint_loc_positions(loc)
    if start is None or end is None:
        return ""
    start_line, start_col = start
    end_line, end_col = end
    if start_line == end_line:
        return f"(L{start_line}:{start_col}〜{end_col})"
    return f"(L{start_line}:{start_col}〜L{end_line}:{end_col})"


def _normalize_whitespace(text: str) -> str:
    """連続するホワイトスペース（改行・タブ・全角空白等）を半角スペース1つに畳んで前後を取り除く。

    JSONL `messages[].msg`を1行に保つ用途で使う。`re.split`をそのまま結合するため、
    複数行に分かれたmsgを意味単位として連結したいケースにも適合する。
    """
    return " ".join(text.split())


def _parse_typos_jsonl(output: str) -> list[ErrorLocation]:
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
                file=pyfltr.paths.to_cwd_relative(str(entry.get("path", ""))),
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


def _parse_glab_ci_lint(output: str) -> list[ErrorLocation]:
    """`glab ci lint`出力をパース。

    glabは行番号を出さないため、検出した各エラーメッセージを`line=1`固定の
    `ErrorLocation`として生成する。

    無効CI出力例::

        Validating...
        .gitlab-ci.yml is invalid

        - jobs:test config contains unknown keys: foo
        - root config contains unknown keys: bar

    有効CI出力では`✓ CI/CD YAML is valid!`のみが流れるため空リストを返す。
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
                file=pyfltr.paths.to_cwd_relative(file_path),
                line=1,
                col=None,
                command="glab-ci-lint",
                message=message,
            )
        )
    return results


def _parse_pytest(output: str) -> list[ErrorLocation]:
    """Pytest出力をパース。`--tb=short`形式のトレースバックからプロジェクト内フレームを優先的に抽出する。

    `_ test_name _`区切りからテスト名を抽出し、message先頭へ`<test_name>: `として併記する。
    pytestの`assert ... == ...`表示はテスト関数名なしでは判別が難しいケースが多く、
    location（file/line）と組み合わせて実質的にnodeid相当の判別性を得るため。
    """
    failures_start = output.find("= FAILURES =")
    summary_start = output.find("short test summary info")
    if failures_start < 0:
        return _parse_with_pattern("pytest", output, _BUILTIN_PATTERNS["pytest"])

    end = summary_start if summary_start > failures_start else len(output)
    failures_section = output[failures_start:end]

    # テスト単位のブロックに分割（`_ test_name _`区切り）。
    # クラスベースのテストでは`_ TestX.test_y _`のようにドット連結された名前が入る。
    block_re = re.compile(r"^_+ (?P<test_name>.+?) _+$", re.MULTILINE)
    block_matches = list(block_re.finditer(failures_section))
    if not block_matches:
        return _parse_with_pattern("pytest", output, _BUILTIN_PATTERNS["pytest"])

    # フレーム行: file:line: in func_name
    frame_re = re.compile(rf"^(?P<file>{_FILE}):(?P<line>\d+): in .+$", re.MULTILINE)
    # エラー行: E   message
    error_re = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)

    results: list[ErrorLocation] = []
    for i, match in enumerate(block_matches):
        start = match.end()
        block_end = block_matches[i + 1].start() if i + 1 < len(block_matches) else len(failures_section)
        block = failures_section[start:block_end]
        test_name = match.group("test_name").strip()

        # フレーム群から最後のプロジェクト内フレームを選択
        frames = list(frame_re.finditer(block))
        if not frames:
            continue

        chosen = frames[-1]  # フォールバック: 最後のフレーム
        for frame in reversed(frames):
            if _is_project_path(pyfltr.paths.to_cwd_relative(frame.group("file"))):
                chosen = frame
                break

        # エラーメッセージ（先頭のE行）
        error_match = error_re.search(block)
        raw_message = error_match.group("message").strip() if error_match else ""
        message = f"{test_name}: {raw_message}" if test_name else raw_message

        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(chosen.group("file")),
                line=int(chosen.group("line")),
                col=None,
                command="pytest",
                message=message,
            )
        )

    if results:
        return results
    # フォールバック: FAILED file::test_nameパターン（line=0）
    return _parse_with_pattern("pytest", output, _BUILTIN_PATTERNS["pytest"])


# コマンド名 -> 関数ベースパーサー。regexで扱いにくい出力（JSONなど）に使う。
_CUSTOM_PARSERS: dict[str, typing.Callable[[str], list[ErrorLocation]]] = {
    "eslint": _parse_eslint_json,
    "ruff-check": _parse_ruff_check_json,
    "pylint": _parse_pylint_json,
    "pyright": _parse_pyright_json,
    "shellcheck": _parse_shellcheck_json,
    "textlint": _parse_textlint_json,
    "typos": _parse_typos_jsonl,
    "pytest": _parse_pytest,
    "glab-ci-lint": _parse_glab_ci_lint,
}


def _summarize_pyright_json(output: str) -> str | None:
    """Pyright --outputjson出力からsummaryフィールドを抽出する。"""
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
    """Pylint --output-format=json2出力からstatisticsフィールドを抽出する。"""
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
    """Pytest出力末尾のサマリー行から = パディングを除去して抽出する。"""
    match = re.search(r"=+ (.+?) =+\s*$", output)
    if match is None:
        return None
    return match.group(1)


# コマンド名 -> サマリーパーサー。JSON出力にサマリーフィールドを持つツールや、
# テキスト出力の整形が必要なツール向け。未登録のテキスト出力ツールは
# `_extract_last_line()`でフォールバックする。
_SUMMARY_PARSERS: dict[str, typing.Callable[[str], str | None]] = {
    "pyright": _summarize_pyright_json,
    "pylint": _summarize_pylint_json,
    "pytest": _summarize_pytest,
}


def _parse_with_pattern(command: str, output: str, pattern: str) -> list[ErrorLocation]:
    """正規表現パターンでエラー箇所をパースする。

    パターンに名前付きグループ`rule`が含まれる場合、マッチ内容を
    `ErrorLocation.rule`に格納し、`rule_urls.build_rule_url()`でURLも補完する。
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
        rule_url = pyfltr.output.rule_urls.build_rule_url(command, rule) if rule is not None else None
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(file_path),
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

    JSON出力（先頭が`[`または`{`）は対象外。区切り線のみの行はスキップする。
    """
    stripped = output.strip()
    if not stripped or stripped[0] in ("[", "{"):
        return None
    for line in reversed(stripped.splitlines()):
        line = line.strip()
        if line and not re.fullmatch(r"[=\-*#]+", line):
            return line
    return None


def _is_project_path(normalized_path: str) -> bool:
    """正規化済みパスがプロジェクト内のファイルかを判定する。

    以下を全て満たす場合にプロジェクト内と見なす:
    - 相対パスである（絶対パスはcwd外 = 標準ライブラリ等）
    - `..`で始まらない（uv管理Pythonの標準ライブラリ等）
    - `.venv/`で始まらない（仮想環境内サードパーティー）
    - `site-packages/`・`dist-packages/`を含まない（名前の異なる仮想環境内サードパーティー）
    """
    if pathlib.PurePosixPath(normalized_path).is_absolute():
        return False
    if normalized_path.startswith(".."):
        return False
    if normalized_path.startswith(".venv/"):
        return False
    return not ("site-packages/" in normalized_path or "dist-packages/" in normalized_path)
