"""エラー出力パーサー。

各コマンドの出力からエラー箇所（ファイル名:行番号）を抽出する。
ビルトインパーサーとカスタム正規表現の両方に対応。
"""

# pylint: disable=too-many-lines

import contextlib
import dataclasses
import json
import pathlib
import re
import typing

import pyfltr.output.github_annotations
import pyfltr.output.rule_urls
import pyfltr.paths


@dataclasses.dataclass
class ErrorLocation:
    """エラー箇所の情報。"""

    file: str
    line: int
    col: int | None
    """違反箇所の列（Noneはツールが列情報を返さない場合）。

    textlintの`column`はノード先頭からの累積位置を返す仕様のため、textlint由来の
    `col`は累積位置として扱う。他ツールは行内オフセットを返す。
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
    """違反範囲の終端行（Noneはツールが範囲を返さない場合）。

    現状はtextlint v12+の`loc.end.line`のみが値を格納する。pyright・biome等にも将来拡張可。
    """
    end_col: int | None = None
    """違反範囲の終端列（Noneはツールが範囲を返さない場合）。

    textlintの`column`系はノード先頭からの累積位置を返す仕様のため、`col`/`end_col`の
    双方とも同様の系で出力する。行内オフセットへの正規化はファイル本文の参照を要するため行わない。
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
    "ja-technical-writing/no-unmatched-pair": (
        "Bracket pair is unmatched. Check for typos or missing pairs, and ensure full-width bracket pairs"
        " do not span across line breaks (the rule treats line breaks as separators)."
    ),
}
"""textlintの頻出ルール向けヒント辞書。利用者が該当しやすいルールに限定している。

ヒント文字列はルール固有の修正観点のみに留める（重複させず、3ルール中1ルールのみが
膨らむのを避けるため）。各`ErrorLocation`の`hint`フィールドに格納され、
`aggregate_diagnostics()`によってcommandレコードの`command.hints`辞書へ集約される。
"""


def parse_errors(
    command: str,
    output: str,
    error_pattern: str | None = None,
    *,
    file_path_remap: dict[str, str] | None = None,
) -> list[ErrorLocation]:
    """コマンド出力からエラー箇所をパースする。

    優先順位:
        1. error_pattern（カスタム正規表現）が指定されていればそれを使用
        2. コマンド専用の関数ベースパーサー（JSON出力などregexで扱いにくいもの）
        3. ビルトイン正規表現パーサー
        4. いずれもなければ空リスト
    """
    if error_pattern is not None:
        return _apply_file_path_remap(_parse_with_pattern(command, output, error_pattern), file_path_remap)
    custom_parser = _CUSTOM_PARSERS.get(command)
    if custom_parser is not None:
        return _apply_file_path_remap(custom_parser(output), file_path_remap)
    builtin = _BUILTIN_PATTERNS.get(command)
    if builtin is not None:
        return _apply_file_path_remap(_parse_with_pattern(command, output, builtin), file_path_remap)
    return []


def _apply_file_path_remap(errors: list[ErrorLocation], remap: dict[str, str] | None) -> list[ErrorLocation]:
    """診断の一時ファイルパスを元ファイルパスへ戻す。"""
    if remap is None:
        return errors
    normalized_remap = dict(remap)
    for temporary_path, original_path in remap.items():
        normalized_remap[pyfltr.paths.to_cwd_relative(temporary_path)] = original_path
        normalized_remap[pyfltr.paths.normalize_separators(pathlib.Path(temporary_path).resolve())] = original_path
    return [
        dataclasses.replace(error, file=pyfltr.paths.to_cwd_relative(original_path))
        if (original_path := normalized_remap.get(_normalize_remap_lookup_path(error.file))) is not None
        else error
        for error in errors
    ]


def _normalize_remap_lookup_path(path: str) -> str:
    """remap照合用に診断ファイルパスを正規化する。"""
    as_path = pathlib.Path(path)
    if as_path.is_absolute() or ".." in as_path.parts:
        return pyfltr.paths.normalize_separators(as_path.resolve())
    return pyfltr.paths.normalize_separators(path)


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
    """エラー箇所を`file:line[:col]: [tool[:rule]] message`のテキスト形式にフォーマットする。

    `severity == "info"`のときはmessage先頭に`[INFO] `を付加し、
    エラーや警告と視覚的に区別する。warning severityは既存のテキスト出力との
    互換性を維持するため表記を変更しない。
    """
    col_str = f":{error.col}" if error.col else ""
    tag = f"{error.command}:{error.rule}" if error.rule else error.command
    message = f"[INFO] {error.message}" if error.severity == "info" else error.message
    return f"{error.file}:{error.line}{col_str}: [{tag}] {message}"


def format_error_github(error: ErrorLocation) -> str:
    """エラー箇所をGitHub Actionsのワークフローコマンド記法にフォーマットする。

    `::error file=...::message`形式で出力する。
    """
    return pyfltr.output.github_annotations.build_workflow_command(error)


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
    # 実行環境により file.md:3 error MD001/... のように severity が介在する。
    # 列を報告するルール（MD059・MD009等）は file.md:3:32 MD059/... のように列番号が介在する。
    # 列番号を任意グループとして許容しないと、`_FILE`のドライブレター表記が
    # `file.md:3`の末尾へ侵入する形でマッチし、file・lineともに架空の値になる。
    # 先頭のMDxxxをruleグループとして抽出する（スラッシュ以降のシンボルはmessageに残す）。
    "markdownlint": rf"(?P<file>{_FILE}):(?P<line>\d+)(?::(?P<col>\d+))?\s+(?:\w+\s+)?(?P<rule>MD\d+)(?P<message>\S*\s+.+)",
    # textlint --format compact出力例: /path/file.md: line 1, col 1, Error - message (rule)
    "textlint": rf"(?P<file>{_FILE}):\s*line\s+(?P<line>\d+),\s*col\s+(?P<col>\d+),\s*\w+\s*-\s*(?P<message>.+)",
    # biome --reporter=github出力例（実機確認済み、lineとcolの間にendLineが介在する）:
    # ::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::Use === instead of ==
    # [^:]*?で順序非依存かつ`::`終端を跨がないようマッチする。
    # biomeはunsafe fix可能なinfo診断を`::notice`として出力する。pyfltr側ではinfoとして公開し、
    # CIの失敗扱いには昇格させない（biome公式設計でinfoは終了コードに影響しない）。
    "biome": (
        r"::(?P<severity>error|warning|notice)\s+[^:]*?file=(?P<file>[^,]+)"
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
    # colloquial-check出力例: src/foo.md:10:5: [match] -> [replacement] excerpt
    # 置換候補が無い場合は矢印以降を省略した`src/foo.md:10:5: [match] excerpt`形式。
    "colloquial-check": rf"(?P<file>{_FILE}):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
}


def _try_json_loads(output: str) -> typing.Any:
    """JSONパースを試みる。失敗時はNoneを返す。

    一部ツール（例: pylint）は`PYTHONDEVMODE=1`環境で読み込んだプラグインの
    `DeprecationWarning`などをJSON本体の前にテキストとして出力する。そのままでは
    パースが必ず失敗するため、先頭の`{`または`[`を見つけて、それ以前の
    不要文字列を除去してから再試行する。
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

    `high` / `medium` / `low`はbanditのseverity表現に対応し、それぞれ
    `error` / `warning` / `info`へマップする。
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
    if lowered in ("error", "fatal", "high"):
        return "error"
    if lowered in ("warning", "warn", "medium"):
        return "warning"
    if lowered in ("info", "information", "informational", "note", "notice", "hint", "style", "convention", "refactor", "low"):
        return "info"
    return None


def _eslint_severity(value: typing.Any) -> str | None:
    """ESLint/textlint の severity 数値を文字列に変換する。"""
    if value == 2:
        return "error"
    if value == 1:
        return "warning"
    return None


_AUDIT_SEVERITY_MAP: dict[str, str] = {
    "critical": "error",
    "high": "error",
    "moderate": "warning",
    "low": "warning",
    "info": "info",
}
"""npm系の脆弱性深刻度（critical / high / moderate / low / info）→ 3値モデルの対応表。

`_normalize_severity`はこれらの語を解釈しないため、依存の脆弱性監査ツール専用の正規化に使う。
"""


def _normalize_audit_severity(value: typing.Any) -> str | None:
    """npm系auditツールの深刻度ラベルを`"error"` / `"warning"` / `"info"`へ正規化する。

    未知の値やNoneは`None`を返し、JSONL出力側で省略される。
    """
    if not isinstance(value, str):
        return None
    return _AUDIT_SEVERITY_MAP.get(value.strip().lower())


def _parse_file_messages_format(
    data: list[dict],
    message_to_location: typing.Callable[[dict, str], ErrorLocation | None],
) -> list[ErrorLocation]:
    """topレベルが`[{filePath, messages:[...]}]`形式のJSON配列を処理する共通ヘルパー。

    eslint / textlint の出力形式に共通する「ファイルごとのmessages配列」構造を処理する。
    各メッセージから`ErrorLocation`へのマッピングは`message_to_location`に委ねる。
    `message_to_location`が`None`を返したメッセージはスキップする。
    """
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
            location = message_to_location(msg, file_path)
            if location is not None:
                results.append(location)
    return results


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

    def _msg_to_location(msg: dict, file_path: str) -> ErrorLocation | None:
        line = msg.get("line")
        if not isinstance(line, int):
            return None
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
        return ErrorLocation(
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

    return _parse_file_messages_format(data, _msg_to_location)


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

    # pylintのmessagesはファイルごとにネストしない平坦な配列のため、直接ループしてErrorLocationを構築する。
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
        return ErrorLocation(
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

    return _parse_file_messages_format(data, _msg_to_location)


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
    取り出した`end_line`/`end_col`はErrorLocationにそのまま格納する。
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


def _parse_designmd_json(output: str) -> list[ErrorLocation]:
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
    data = _try_json_loads(output)
    if not isinstance(data, dict):
        return []
    findings = data.get("findings", [])
    if not isinstance(findings, list):
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
                severity=_normalize_severity(entry.get("severity")),
            )
        )
    return results


def _parse_lychee_json(output: str) -> list[ErrorLocation]:
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

    `error_map`は「ファイルパス → エラーレスポンス配列」のmap。各エラーから`url`/`status.text`を抽出し、
    `ErrorLocation.message`へ整形する。lycheeのJSONには行情報を含まないため`line=1`固定とする。
    JSON解析失敗時は空リストを返す。
    """
    data = _try_json_loads(output)
    if not isinstance(data, dict):
        return []
    error_map = data.get("error_map", {})
    if not isinstance(error_map, dict):
        return []
    results: list[ErrorLocation] = []
    for file_path, entries in error_map.items():
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
                    file=pyfltr.paths.to_cwd_relative(str(file_path)),
                    line=1,
                    col=None,
                    command="lychee",
                    message=message,
                    severity="error",
                )
            )
    return results


def _parse_semgrep_json(output: str) -> list[ErrorLocation]:
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
    data = _try_json_loads(output)
    if not isinstance(data, dict):
        return []
    raw_results = data.get("results", [])
    if not isinstance(raw_results, list):
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
        extra = entry.get("extra", {}) if isinstance(entry.get("extra"), dict) else {}
        rule = str(entry.get("check_id", "") or "") or None
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(str(entry.get("path", ""))),
                line=line,
                col=col,
                command="semgrep",
                message=str(extra.get("message", "") or ""),
                rule=rule,
                severity=_normalize_severity(extra.get("severity")),
            )
        )
    return results


def _parse_bandit_json(output: str) -> list[ErrorLocation]:
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
    data = _try_json_loads(output)
    if not isinstance(data, dict):
        return []
    raw_results = data.get("results", [])
    if not isinstance(raw_results, list):
        return []
    results: list[ErrorLocation] = []
    for entry in raw_results:
        if not isinstance(entry, dict):
            continue
        line = entry.get("line_number")
        if not isinstance(line, int):
            continue
        raw_col = entry.get("col_offset")
        col = raw_col if isinstance(raw_col, int) else None
        message = str(entry.get("issue_text", "") or "")
        more_info = entry.get("more_info")
        if isinstance(more_info, str) and more_info:
            message = f"{message} (see {more_info})" if message else f"(see {more_info})"
        rule = str(entry.get("test_id", "") or "") or None
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(str(entry.get("filename", ""))),
                line=line,
                col=col,
                command="bandit",
                message=message,
                rule=rule,
                severity=_normalize_severity(entry.get("issue_severity")),
            )
        )
    return results


def _parse_sqlfluff_json(output: str) -> list[ErrorLocation]:
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
    data = _try_json_loads(output)
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
            rule = str(violation.get("code", "") or "") or None
            severity = "warning" if violation.get("warning") else "error"
            results.append(
                ErrorLocation(
                    file=pyfltr.paths.to_cwd_relative(file_path),
                    line=line,
                    col=col,
                    command="sqlfluff",
                    message=str(violation.get("description", "") or ""),
                    rule=rule,
                    severity=severity,
                )
            )
    return results


_GHSA_RE = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}", re.IGNORECASE)
"""advisory URLからGitHub Security Advisory識別子（GHSA-xxxx-xxxx-xxxx）を抽出する正規表現。"""


def _extract_advisory_rule(url: str, fallback_id: typing.Any) -> str | None:
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


def _parse_uv_audit(output: str) -> list[ErrorLocation]:
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


def _parse_npm_audit_json(output: str) -> list[ErrorLocation]:
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
    data = _try_json_loads(output)
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
                    rule=_extract_advisory_rule(url, source),
                    severity=_normalize_audit_severity(via.get("severity")),
                    rule_url=url or None,
                )
            )
    return results


def _parse_pnpm_audit_json(output: str) -> list[ErrorLocation]:
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
    data = _try_json_loads(output)
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
                rule=ghsa or _extract_advisory_rule(url, advisory.get("id")),
                severity=_normalize_audit_severity(advisory.get("severity")),
                rule_url=url or None,
            )
        )
    return results


def _parse_yarn_audit_jsonl(output: str) -> list[ErrorLocation]:
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
                rule=ghsa or _extract_advisory_rule(url, advisory_id),
                severity=_normalize_audit_severity(advisory.get("severity")),
                rule_url=url or None,
            )
        )
    return results


def _parse_glab_ci_lint(output: str) -> list[ErrorLocation]:
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
                file=pyfltr.paths.to_cwd_relative(file_path),
                line=1,
                col=None,
                command="glab-ci-lint",
                message=message,
            )
        )
    return results


def _parse_vitest_json(output: str) -> list[ErrorLocation]:
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
    data = _try_json_loads(output)
    if not isinstance(data, dict):
        return []
    test_results = data.get("testResults", [])
    if not isinstance(test_results, list):
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
                    file=pyfltr.paths.to_cwd_relative(file_path),
                    line=line,
                    col=col,
                    command="vitest",
                    message=message,
                )
            )
    return results


_PYTEST_SUMMARY_RE = re.compile(
    rf"^FAILED\s+(?P<file>{_FILE})::(?P<test>[^\s\[]+(?:\[.*?\])?)(?:\s+-\s+(?P<message>.+))?$",
    re.MULTILINE,
)
_PYTEST_CRASH_RE = re.compile(rf"worker '(?P<worker>[^']+)' crashed while running '(?P<file>{_FILE})::(?P<test>[^']+)'")
_PYTEST_TB_LINE_RE = re.compile(
    rf"^(?P<file>{_FILE}):(?P<line>\d+):\s*(?P<message>.+)$",
    re.MULTILINE,
)
# 失敗欄のブロック見出し（`_____ <テスト名> _____`）。
# 見出し名へ`_`・空白以外の文字を1つ以上要求する。既定のトレースバック形式が例外の連鎖の
# エントリー間へ出力する区切り行は`_ `の反復で、`_pytest/_io/terminalwriter.py`の`sep`が
# 行幅の余りへ`_`を1文字足すため、行幅が奇数だと`_ _ ... _ _`の形になる。
# 見出し名を`.+?`のままにすると当該区切り行がブロック見出しとして一致し、多段フレームの失敗が
# 区切り行で分割されて診断が水増しされる。行幅はWindowsで常に奇数側へ寄る
# （同ファイルの`sep`が`win32`で`fullwidth`を1減らす）ため、例外的な条件ではない。
_PYTEST_BLOCK_HEAD_RE = re.compile(r"^_+ (?P<test_name>.*?[^\s_].*?) _+$", re.MULTILINE)
_PYTEST_CAPTURED_SECTION_RE = re.compile(r"^-+ Captured .+ -+$", re.MULTILINE)
# 既定のトレースバック形式（`--tb=auto`・`--tb=long`）が各エントリーの末尾へ出力する位置行。
# 例外を送出したエントリーは例外名を伴い（`sample_test.py:6: AssertionError`）、
# 呼び出し側のエントリーは例外名を持たない（`sample_test.py:9: `）。
# `--tb=short`のフレーム行（`file:line: in func`）と違い関数名を伴わないため、
# フレーム解析だけでは拾えない。両形を1つの正規表現で拾い、選択側で例外名の有無を判別する。
_PYTEST_LOCATION_LINE_RE = re.compile(rf"^(?P<file>{_FILE}):(?P<line>\d+):(?P<message>.*)$", re.MULTILINE)
_PYTEST_SESSION_START_RE = re.compile(r"^=+ test session starts =+$")
_PYTEST_SESSION_END_RE = re.compile(r"^=+ .* in \d+(?:\.\d+)?s.*=+$")
# `-q`で起動したpytestの最終集計行は`=`の埋めを伴わない（例: `2 failed in 0.92s`）。
# 埋めが無い分だけ任意のテキスト行と紛れやすいため、pytestが実際に組み立てる形へ厳密に合わせる。
# 件数と分類の並びは`_pytest/terminal.py`が`"%d %s"`を`", "`で連結する形、
# 秒数の表記は同ファイルの`format_session_duration`が返す`<秒数>s`と
# 60秒以上での`<秒数>s (<経過時間>)`の2形に対応する。
# 末尾を`.*`で開いたままにすると`12 files processed in 2.5s`のような行が上限として採られ、
# 上限が親の集計行を越えて親の失敗ごと除外する事故を招く。
# `--collect-only`の集計行（`6 tests collected in 0.01s`等）は対象外とする。
# 当該実行は失敗の診断を生成しないため上限を必要としない。
_PYTEST_QUIET_SESSION_END_RE = re.compile(
    r"^(?:\d+ \w+(?:, \d+ \w+)*|no tests ran) in \d+(?:\.\d+)?s(?: \((?:\d+ days?, )?\d+:\d{2}:\d{2}\))?$"
)
# 失敗一覧の見出し。`_pytest/terminal.py`の`short_test_summary`が`write_sep("=", ...)`で
# 出力するため常に`=`の埋めを伴う。部分文字列の探索にすると、テストの捕捉出力が
# 当該語句を含むだけの行を見出しとして採る。
_PYTEST_SUMMARY_HEAD_RE = re.compile(r"^=+ short test summary info =+$")
# 失敗一覧の見出しより後に現れると、当該見出しが親自身のものではないことを示す標識。
# `_pytest/terminal.py`は失敗欄・失敗一覧・最終集計行をこの順で出力し、失敗一覧と
# 最終集計行の間には追加分の警告の集計（`=+ warnings summary =+`とその本文）と、
# 中断・停止の報告（`_report_keyboardinterrupt`が出力する`!+ KeyboardInterrupt !+`と
# そのトレースバック）以外は現れない。したがって、失敗欄のブロック見出し・捕捉出力の節見出し・
# 実行の開始行と終了集計行・別の失敗一覧の見出しのいずれかが後続する見出しは、
# 捕捉出力へ混入した子プロセスのものである。
# 警告の集計と中断・停止の報告の見出し・本文は標識に含めない。含めると、これらを伴う実出力で
# 親自身の失敗一覧を子のものと誤判定し、失敗一覧のみを情報源とする失敗を丸ごと失う。
_PYTEST_NESTED_RUN_MARKER_RES = (
    _PYTEST_BLOCK_HEAD_RE,
    _PYTEST_CAPTURED_SECTION_RE,
    _PYTEST_SESSION_START_RE,
    _PYTEST_SESSION_END_RE,
    _PYTEST_QUIET_SESSION_END_RE,
    _PYTEST_SUMMARY_HEAD_RE,
)


def _pytest_parent_summary_start(output: str) -> int:
    """親プロセス自身の失敗一覧の見出しの開始位置を返す。見出しを持たない場合は-1を返す。

    pytestを子プロセスとして起動し出力を取り込むテストでは、捕捉出力の内側へ子プロセスの
    失敗一覧の見出しがそのまま現れる。出力中で最後に現れる見出しを無条件に採ると、親が
    失敗一覧を出力しない構成（`-rN`等）で子の見出しを親のものとして採用し、失敗欄の解析範囲が
    そこで打ち切られる。打ち切り以降にある親の実在する失敗は診断から消え、代わりに子の失敗が
    親の失敗として報告される。

    判別は、当該見出しより後・親の最終集計行より前に入れ子の実行を示す標識
    （`_PYTEST_NESTED_RUN_MARKER_RES`）が現れないことによる。親は失敗一覧を最後の節として
    出力するため、標識が後続する見出しは親のものではない。候補は親の最終集計行より前の
    見出しに限り、末尾側から順に判定して最初に条件を満たしたものを採用する。

    上限として採った集計行より後に標識が現れる場合、当該集計行は親自身の最終集計行ではなく
    捕捉出力へ混入した子のものである。親が最終集計行を持たないまま出力が終わる構成で起こる。
    この場合は上限を出力の末尾へ広げて探し直す。上限を確定できないことを理由に判別を諦めると、
    親自身の失敗一覧を持つ構成でも失敗一覧が空となり、捕捉出力へ残った子の失敗を除外する
    安全網が働かなくなる。

    上限より後の走査は実行の開始行に達した時点で打ち切る。pyfltrは子孫プロセスの出力を
    ストリームの終端まで読むため、親の実行が終わった後に打ち切られた孫プロセスの実行が
    同じ出力へ続くことがある。当該実行の標識で上限を広げると、失敗一覧のみを情報源とする
    構成（`--tb=no`等）で親の失敗をすべて失う。

    子プロセスが終了集計行も後続の親の失敗欄も持たないまま出力の末尾に達する構成では、
    子の見出しが条件を満たし親のものとして採られる。当該構成は除外の主経路も安全網も
    成立しない既知の縮退であり、判別条件の追加で新たに生じるものではない。
    """
    lines = output.split("\n")
    limit = _pytest_parent_tail_index(lines)
    for following in range(limit + 1, len(lines)):
        if _PYTEST_SESSION_START_RE.fullmatch(lines[following].rstrip("\r")):
            break
        if _is_pytest_nested_run_marker(lines[following]):
            limit = len(lines)
            break
    heads = [index for index, line in enumerate(lines[:limit]) if _PYTEST_SUMMARY_HEAD_RE.fullmatch(line.rstrip())]
    for index in reversed(heads):
        if any(_is_pytest_nested_run_marker(lines[following]) for following in range(index + 1, limit)):
            continue
        return sum(len(line) + 1 for line in lines[:index])
    return -1


def _is_pytest_nested_run_marker(line: str) -> bool:
    """行が入れ子の実行を示す標識かを返す。

    末尾の空白を除去せずに照合する。既定のトレースバック形式が例外の連鎖のエントリー間へ
    出力する区切り行（`_ _ _ ... _ `）は末尾に空白を伴い、ブロック見出しの照合
    （`_PYTEST_BLOCK_HEAD_RE`の行末の`$`）では一致しない。空白を除去すると当該区切り行が
    ブロック見出しとして一致し、`--full-trace`付きの中断のように失敗一覧より後へ完全な
    トレースバックが続く出力で、親自身の失敗一覧を子のものと誤判定する。
    """
    return any(pattern.fullmatch(line.rstrip("\r")) for pattern in _PYTEST_NESTED_RUN_MARKER_RES)


def _has_pytest_summary_head(output: str) -> bool:
    """出力が失敗一覧の見出しを含むかを返す。親のものか子のものかは区別しない。"""
    return any(_PYTEST_SUMMARY_HEAD_RE.fullmatch(line.rstrip()) for line in output.split("\n"))


def _parse_pytest_summary(output: str) -> dict[tuple[str, str], str | None]:
    r"""`short test summary info`の`FAILED <file>::<test> - <message>`行を解析する。

    キーは`(file, test)`。`test`部分は`(?P<test>[^\s\[]+(?:\[.*?\])?)`で、パラメータ化テストの
    角括弧内には空白・ハイフン・角括弧の入れ子を許容する（`test_a[param with space]`・
    `test_param[b - c]`・`test_listid[['a', 'b']]`・`test_nested[list[int] and str]`のように、
    `ids`へリストや型注釈風の文字列を渡すと実際に生成されるIDである）。角括弧の外側は
    空白と`[`を含まないnodeidの制約を`[^\s\[]+`で表し、角括弧内は`\[.*?\]`の非貪欲マッチと
    後続文脈（`\s+-\s+`または行末）へのバックトラックで閉じ位置を確定する。
    `[^\]]*`のように閉じ括弧を越えられない表現にすると、閉じ括弧や入れ子を含むIDの行が
    一切マッチせず、失敗一覧のみが情報源となる`--tb=no`等で当該失敗が診断から消える。
    `test`は`::`区切り（`TestX::test_y`形式、pytestのnodeid表記）を
    `.`区切り（`TestX.test_y`形式、`= FAILURES =`セクションのブロック見出しの表記）へ
    `.replace("::", ".")`で正規化する。両表記が一致しないとテスト名突合（`consumed`集合）が
    成立せず、summary残余補完で同一テストの診断が二重生成されるため。値は`message`
    （省略時はNone、`--tb=line`経路の値と突合できるよう前後の空白・改行文字を`.strip()`で
    除去して統一する）。走査範囲の決定は呼び出し側が担う（`_parse_pytest`が
    `_pytest_parent_summary_start`で親自身の失敗一覧の見出しを特定し、当該見出し以降のみを
    渡す）。テストの捕捉出力に子プロセスpytestの`FAILED ...`行が混入していても、
    実在しない失敗として誤検出しないため。
    """
    summary: dict[tuple[str, str], str | None] = {}
    for match in _PYTEST_SUMMARY_RE.finditer(output):
        file_path = pyfltr.paths.to_cwd_relative(match.group("file"))
        test_name = match.group("test").replace("::", ".")
        raw_message = match.group("message")
        summary[(file_path, test_name)] = raw_message.strip() if raw_message is not None else None
    return summary


def _parse_pytest_from_summary(
    summary: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]]
) -> list[ErrorLocation]:
    """summary辞書のうち他経路で拾えなかった残余エントリを`line=0`の診断として補完する。

    `consumed`はsummary辞書のキーそのもの（`(file, test)`の組）の集合である。
    キーの決定は`_consume_summary_test`が担い、フレーム選択がプロジェクト外フレーム
    （`.venv/`配下等）へフォールバックし診断の`file`とsummaryの`file`が一致しない場合でも、
    同名テストの未消費候補へフォールバックして消費するため、ファイルパス込みキーでも
    二重生成を招かない。同名テストが別ファイルに存在する場合はファイル込みキーにより
    それぞれ独立したエントリとして扱われ、取りこぼしを防ぐ。
    """
    results: list[ErrorLocation] = []
    for (file_path, test_name), message in summary.items():
        if (file_path, test_name) in consumed:
            continue
        raw_message = message or ""
        results.append(
            ErrorLocation(
                file=file_path,
                line=0,
                col=None,
                command="pytest",
                message=f"{test_name}: {raw_message}" if raw_message else test_name,
            )
        )
    return results


def _consume_summary_test(
    summary: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
    test_name: str,
    diagnosed_file: str,
) -> None:
    """ブロック解析等で確定したテスト名をsummary辞書と突合し、対応する1件を消費済みにする。

    同名テストが複数ファイルに存在し得るため、診断ファイル（`diagnosed_file`）と一致する候補を
    優先して消費する。一致する候補が無い場合（フレーム選択がプロジェクト外へフォールバックし
    診断ファイルとテスト本来のファイルが一致しない等）は、同名の未消費候補を1件先頭から選んで
    消費する。マッチが1件も無い場合は何もしない（summaryに対応エントリが無い、または
    `= FAILURES =`セクションのみでsummaryが空の場合）。
    """
    fallback_key: tuple[str, str] | None = None
    for key in summary:
        sum_file, sum_test = key
        if sum_test != test_name or key in consumed:
            continue
        if sum_file == diagnosed_file:
            consumed.add(key)
            return
        if fallback_key is None:
            fallback_key = key
    if fallback_key is not None:
        consumed.add(fallback_key)


def _match_truncated_summary(
    summary: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]], message: str
) -> tuple[str, str] | None:
    """切り詰められた集計行のメッセージを位置行のメッセージへ前方一致で突合する。

    pytestの集計行は失敗理由が端末幅に収まらないとき末尾を`...`へ置き換えて出力する。
    完全一致だけで突合すると当該の失敗が`consumed`へ登録されず、位置行由来の診断と
    残余補完による`line=0`の診断が同一の失敗に対して二重生成される。

    前方一致は別々の失敗が同じ接頭辞を持つ場合に取り違えるため、切り詰めを示す`...`で
    終わる候補に限定し、未消費の候補が複数一致する場合は突合しない。
    """
    matched = [
        key
        for key, sum_message in summary.items()
        if key not in consumed
        and sum_message is not None
        and sum_message.endswith("...")
        and message.startswith(sum_message[: -len("...")])
    ]
    return matched[0] if len(matched) == 1 else None


def _mask_pytest_captured_child_runs(output: str) -> str:
    """テストの捕捉出力へ混入した子プロセスのpytest実行を空行へ置き換える。

    pytestを子プロセスとして起動し出力を取り込むテストでは、親の失敗欄・集計行の内側へ
    子プロセスの失敗欄・集計行がそのまま現れる。除外しないと子プロセス側の失敗が
    親プロセスの実在する失敗として診断化され、存在しないファイル・行番号が報告される。

    除外する領域は、捕捉出力の節見出し（`-+ Captured .+ -+`）より後に現れた
    子プロセスの実行開始行（`=+ test session starts =+`）から、対応する終了集計行
    （`=+ ... in <秒数>s ... =+`）までとする。開始・終了の双方をpytest自身が出力する
    マーカーで判定するため、除外範囲は子プロセスの実行1回分に正確に一致する。

    子プロセスのブロック見出し・位置行・捕捉出力の節見出しは親のものと書式が同一で、
    構文だけでは判別できない。
    親の集計行との突合で終端を判定すると、親子で同名のテストが失敗する場合や、
    ブロック見出しを持たない`--tb=line`形式で終端を決められず、本来の失敗まで除外する。
    実行マーカーによる判定はこれらの構成に依存しない。

    終了集計行を`_pytest_parent_tail_index`が返す上限より前に見つけられない場合、当該領域は
    除外しない。子プロセスが異常終了・打ち切りで終了集計行を欠いたまま終わると、親自身の
    最終集計行を終端として誤採用し、その間にある親の失敗と失敗一覧をすべて失うため、
    上限を越える終端候補は採用せず旧来の挙動へ縮退させる。
    捕捉出力が子プロセスのpytest実行を含まない場合も同じ理由で除外しない。

    次の3つの構成では開始・終了のマーカーが成立せず除外できない。いずれも子プロセス側の
    失敗が親の失敗として混入するが、本処理の導入前と同じ結果であり退行にはあたらない。
    親が失敗一覧を出力し、かつ子のテスト名が親の失敗一覧に無い場合は`_parse_pytest`の
    安全網（失敗一覧に載らないテスト名の失敗欄を除外する）が働き、架空の診断が残らない。
    親子で同名のテストが失敗する構成では安全網も働かない。

    - 子プロセスを`-q`で起動した場合。実行開始行を出力しないため開始位置を確定できない
    - 親プロセスが`-s`で動く場合。子の出力が捕捉されず節見出しが出ないうえ、
      子の実行開始行が親の進捗行と同一行へ連結される
    - 子プロセス側の失敗したテストが出力を持つ場合。子自身の捕捉出力の節見出しが
      親の節見出しと同一書式で現れ、終端未確定の開始位置を破棄する規則が発火する。
      破棄規則を子の失敗欄より後で止めると、未終端の実行が親のブロック境界を越えて
      後続の節の終了集計行と対になり、間にある親の失敗を除外する側の誤りへ倒れる

    行を削除せず空行へ置き換えるのは、以降の正規表現探索が扱う行構造を保つためである。
    """
    # 行の分割は`\n`のみを区切りとする。`str.splitlines`はフォームフィード等でも分割するため、
    # 以降の正規表現探索（`re.MULTILINE`は`\n`のみを行区切りとする）と行の対応が崩れる。
    lines = output.split("\n")
    limit = _pytest_parent_tail_index(lines)
    masked = [False] * len(lines)
    after_captured = False
    start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if _PYTEST_CAPTURED_SECTION_RE.fullmatch(stripped):
            after_captured = True
            # 1回の実行の出力が2つの捕捉出力の節へまたがることはないため、節の切れ目で
            # 終端未確定の開始位置を破棄する。破棄しないと、別の節に現れた終了集計行
            # （子の出力の末尾だけを表示した場合など）と対になり、間の親の失敗を除外する。
            start = None
            continue
        if after_captured and _PYTEST_SESSION_START_RE.fullmatch(stripped):
            # 終端未確定のまま次の実行開始行に達した場合は、そちらへ開始位置を移す。
            # 終了集計行を欠いた実行の開始位置を保持したままにすると、後続の別の実行の
            # 終了集計行と対になり、その間にある親の失敗まで除外してしまう。
            start = i
            continue
        if start is None:
            continue
        if i >= limit:
            # 上限へ到達した領域は終端を確定できなかったものとして扱い、除外しない。
            start = None
            continue
        if _PYTEST_SESSION_END_RE.fullmatch(stripped):
            for j in range(start, i + 1):
                masked[j] = True
            start = None
    return "\n".join("" if is_masked else line for is_masked, line in zip(masked, lines, strict=True))


def _pytest_parent_tail_index(lines: list[str]) -> int:
    """親プロセス自身の最終集計行の位置を返す。子プロセスの実行を除外する範囲の上限として使う。

    最終集計行のうち末尾のものを採用する。親の最終集計行は親の失敗欄・失敗一覧より後に出るため、
    捕捉出力へ混入した子プロセスの集計行は必ずこれより前に位置する。末尾側を採用しないと
    親の失敗欄の途中で上限に達し、子プロセスの除外が成立しなくなる。

    親が`-q`で動く場合の最終集計行は`=`の埋めを伴わないため、当該形式も併せて探す。
    `=`の埋めを伴う形式だけを探すと、`-q`の親では出力中で最後に一致するのが
    捕捉出力へ混入した子の集計行となり、上限が子の終端そのものを指して除外が成立しなくなる。

    最終集計行を持たない構成では上限を設けない。
    """
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].rstrip()
        if _PYTEST_SESSION_END_RE.fullmatch(stripped) or _PYTEST_QUIET_SESSION_END_RE.fullmatch(stripped):
            return i
    return len(lines)


def _select_pytest_location_line(block: str, *, allow_fallback: bool) -> re.Match[str] | None:
    """失敗ブロックから既定のトレースバック形式の位置行を選ぶ。

    既定のトレースバック形式（`--tb=auto`・`--tb=long`）は各エントリーの末尾へ位置行を
    出力する。例外を送出したエントリーは例外名を伴い、呼び出し側のエントリーは伴わない。
    フレームが1つだけの失敗（`assert`直書きの典型的な失敗）ではフレーム行
    （`<file>:<line>: in <func>`）が現れないため、位置行が唯一の行番号の情報源となる。

    走査範囲は捕捉出力の節見出しより前に限る。節より後は任意のテキストであり、
    位置行と同じ書式の行が現れても失敗の位置ではない。

    例外の連鎖では例外名を伴う位置行が複数現れる。最後のものが実際に失敗を起こした例外の
    位置であり、集計行のメッセージとも一致する。プロジェクト内のものを優先して末尾側から
    選ぶのはフレーム解析と同じ方針による。

    `allow_fallback`はブロックがフレーム行を持たない場合に`True`とする。この場合は他に
    行番号の情報源が無いため、プロジェクト内の位置行が例外名を伴わないものしか無ければ
    それを採用し（`--tb=short`が選ぶプロジェクト内フレームと同じ位置を指す）、
    プロジェクト内の位置行が皆無ならプロジェクト外の位置行まで採る。
    フレーム行を持つ場合は`False`とし、プロジェクト内フレームを優先する選択を崩さない。
    """
    section = _PYTEST_CAPTURED_SECTION_RE.search(block)
    scan_target = block[: section.start()] if section is not None else block
    typed: list[re.Match[str]] = []
    bare: list[re.Match[str]] = []
    for match in _PYTEST_LOCATION_LINE_RE.finditer(scan_target):
        (typed if match.group("message").strip() else bare).append(match)
    for candidates in (typed, bare) if allow_fallback else (typed,):
        for match in reversed(candidates):
            if _is_project_path(pyfltr.paths.to_cwd_relative(match.group("file"))):
                return match
    return typed[-1] if typed and allow_fallback else None


def _pytest_block_message(block: str, error_re: re.Pattern[str], location: re.Match[str]) -> str:
    """位置行に対応するエラー行（`E   <message>`）の本文を返す。

    例外の連鎖では複数のエントリーがブロック内に並ぶ。直前の位置行より後にある最初の
    エラー行が当該エントリーの例外を表すため、そこから採る。直前の位置行が無い場合
    （エントリーが1つだけの場合）はブロック先頭から探す。
    """
    previous_end = 0
    for match in _PYTEST_LOCATION_LINE_RE.finditer(block[: location.start()]):
        previous_end = match.end()
    error_match = error_re.search(block, previous_end) or error_re.search(block)
    return error_match.group("message").strip() if error_match is not None else ""


def _pytest_join_message(test_name: str, raw_message: str) -> str:
    """テスト名と失敗理由の本文を診断のメッセージへ組み立てる。

    pytestの`assert ... == ...`表示はテスト関数名なしでは判別が難しいため、本文の先頭へ
    テスト名を併記する。doctestのように本文（`E   <message>`行）を持たない失敗では
    テスト名のみとする（`<名前>: `で終わる中身の無いメッセージにしない）。
    """
    if not test_name:
        return raw_message
    return f"{test_name}: {raw_message}" if raw_message else test_name


def _is_unlisted_child_block(known_tests: set[str] | None, *, after_captured: bool, test_name: str) -> bool:
    """失敗欄のブロックが親の失敗ではない（捕捉出力へ混入した子プロセスのもの）かを判定する。

    子プロセスの失敗欄は必ず親の捕捉出力の節見出しより後に現れ、親の失敗一覧には載らない。
    両方を満たすブロックのみ親の失敗ではないと判定する。節見出しより前のブロックを
    対象に含めると、親自身が最終集計行を持たず子の失敗一覧だけが出力に残る構成で、
    親の失敗欄をすべて除外する。

    `known_tests`がNoneの構成（親の失敗一覧を特定できない場合）では判定しない。
    """
    return known_tests is not None and after_captured and test_name not in known_tests


def _parse_pytest(output: str) -> list[ErrorLocation]:
    """Pytest出力をパース。

    次の優先順で情報源を扱い、いずれの経路でも失敗理由の本文を可能な限り保持する。

    1. `short test summary info`の`FAILED <file>::<test> - <message>`行を
       `(file, test) -> message`辞書として先に収集する（メッセージ補完・突合用）。
       `test`は`.`区切りへ正規化する。見出しは`_pytest_parent_summary_start`で
       親自身のものを特定し、当該見出し以降のみを走査する
    2. `= FAILURES =`セクションをテスト名区切り（`_ 名前 _`）でブロック分割し、
       既定のトレースバック形式（`--tb=auto`・`--tb=long`）の位置行
       （`<file>:<line>: <例外名>`、フレーム行より後にあるもの）を優先し、
       無ければフレーム行（`file:line: in func`）をプロジェクト内フレーム優先で診断化する
    3. いずれも持たないブロック（xdistワーカークラッシュ等）は
       `worker '<id>' crashed while running '<file>::<test>'`行から`line=0`の診断を生成する
    4. ブロック分割できない場合（`--tb=line`形式）は`<file>:<line>: <message>`行を
       実際の行番号付きで診断化し、summary辞書に同一`(file, message)`があればテスト名を先頭へ併記する
    5. 上記いずれでも拾えなかったsummary辞書の残余エントリを`_parse_pytest_from_summary`で
       `line=0`の診断として補完する

    経路1の前に、捕捉出力へ混入した子プロセスのpytest実行を
    `_mask_pytest_captured_child_runs`で空行へ置き換える。pytestを子プロセスとして起動し
    出力を取り込むテストでは、親の失敗欄・集計行の内側へ子の失敗欄・集計行が現れるため、
    除外しないと子プロセス側の失敗を親プロセスの実在する失敗として診断化する。
    当該除外が成立しない構成に備え、経路2〜3では親の失敗一覧に載らないテスト名のブロックを
    除外する安全網を併用する（失敗一覧の見出しを持たない構成・失敗一覧が空の構成では働かせない）。
    親が失敗一覧を出力しない構成では当該安全網も働かないため、捕捉出力へ残った子の失敗が
    親の失敗として診断化される。子の失敗一覧との突合で除外する案は、親が子と同じテストファイルの
    同名テストを実行する構成において親の実在する失敗を除外するため採らない。

    経路2〜4でテスト名を確定できたつど`_consume_summary_test`で`consumed`
    （summary辞書のキー`(file, test)`の集合）へ登録し、経路5の二重生成を防ぐ。
    同名テストが複数ファイルに存在する場合は診断ファイルと一致する候補を優先して消費し、
    一致が無い場合（フレーム選択がプロジェクト外へフォールバックした場合等）は同名の
    未消費候補へフォールバックする。`_ test_name _`区切りからテスト名を抽出し、message先頭へ
    `<test_name>: `として併記する。pytestの`assert ... == ...`表示はテスト関数名なしでは
    判別が難しいケースが多く、location（file/line）と組み合わせて実質的にnodeid相当の
    判別性を得るため。
    """
    output = _mask_pytest_captured_child_runs(output)

    # 失敗欄の開始位置は先頭側を探す。子プロセスのpytestを起動して出力を取り込むテストでは
    # 捕捉出力の中にも同じ見出しが現れるため、自プロセスのものが先に現れる先頭一致を採用する。
    # 失敗一覧の見出しは`_pytest_parent_summary_start`で親自身のものを特定する。
    failures_start = output.find("= FAILURES =")
    summary_start = _pytest_parent_summary_start(output)

    # 親自身の失敗一覧を持たず子プロセスのものだけが混入している場合、出力中の`FAILED`行は
    # すべて子のものであり親の失敗一覧は存在しない。当該行を親の失敗一覧として扱うと
    # 実在しない失敗を診断化するため、親の失敗一覧は空とする。
    # 見出しが1つも無い構成では出力全体を走査する（見出しを欠く構成への耐性を維持するため）。
    if summary_start >= 0:
        summary = _parse_pytest_summary(output[summary_start:])
    elif _has_pytest_summary_head(output):
        summary = {}
    else:
        summary = _parse_pytest_summary(output)
    consumed: set[tuple[str, str]] = set()

    if failures_start < 0:
        return _parse_pytest_from_summary(summary, consumed)

    end = summary_start if summary_start > failures_start else len(output)
    failures_section = output[failures_start:end]

    block_matches = list(_PYTEST_BLOCK_HEAD_RE.finditer(failures_section))

    if not block_matches:
        # ブロック区切りが無い場合（`--tb=line`形式）: `<file>:<line>: <message>`行を直接拾う。
        # 突合は2段構成とする。1段目はファイル・メッセージの両方が一致する候補、
        # 見つからない場合の2段目はメッセージのみ一致する候補へフォールバックする
        # （位置行がsite-packages等の外部パスになる失敗はファイルが一致しないため）。
        results: list[ErrorLocation] = []
        for match in _PYTEST_TB_LINE_RE.finditer(failures_section):
            file_path = pyfltr.paths.to_cwd_relative(match.group("file"))
            message = match.group("message").strip()
            matched_key: tuple[str, str] | None = None
            for key, sum_message in summary.items():
                if key in consumed:
                    continue
                if key[0] == file_path and sum_message == message:
                    matched_key = key
                    break
            if matched_key is None:
                for key, sum_message in summary.items():
                    if key in consumed:
                        continue
                    if sum_message == message:
                        matched_key = key
                        break
            if matched_key is None:
                matched_key = _match_truncated_summary(summary, consumed, message)
            test_name = matched_key[1] if matched_key is not None else None
            if matched_key is not None:
                consumed.add(matched_key)
            results.append(
                ErrorLocation(
                    file=file_path,
                    line=int(match.group("line")),
                    col=None,
                    command="pytest",
                    message=f"{test_name}: {message}" if test_name else message,
                )
            )
        results.extend(_parse_pytest_from_summary(summary, consumed))
        return results

    # フレーム行: file:line: in func_name
    frame_re = re.compile(rf"^(?P<file>{_FILE}):(?P<line>\d+): in .+$", re.MULTILINE)
    # エラー行: E   message
    error_re = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)

    # 親の失敗一覧に載らないテスト名の失敗欄は親の失敗ではない。除外の主経路（実行マーカーに
    # よる対応付け）が成立しない構成で捕捉出力へ残った子プロセスの失敗欄を除外する安全網とする。
    # 失敗一覧の見出しを持たない構成では`_parse_pytest_summary`が出力全体を走査し、
    # 捕捉出力に混入した子の`FAILED`行まで拾うため、当該構成では安全網を働かせない。
    # 集計行が失敗を列挙しない構成（`-r`の指定から`f`を外した場合など）でも
    # 親の失敗欄をすべて除外しないよう、失敗一覧が空の場合は働かせない。
    known_tests = {test for _, test in summary} if summary and summary_start >= 0 else None
    first_captured = _PYTEST_CAPTURED_SECTION_RE.search(failures_section)

    results = []
    for i, match in enumerate(block_matches):
        start = match.end()
        block_end = block_matches[i + 1].start() if i + 1 < len(block_matches) else len(failures_section)
        block = failures_section[start:block_end]
        # doctestのブロック見出しは`[doctest] <モジュール>.<関数>`形式で、集計行の
        # テスト名（`<モジュール>.<関数>`）と一致しない。接頭辞を除いてから突合しないと
        # summary残余補完で同一の失敗の診断が二重生成される。
        test_name = match.group("test_name").strip().removeprefix("[doctest] ")
        after_captured = first_captured is not None and match.start() > first_captured.start()

        frames = list(frame_re.finditer(block))
        location = _select_pytest_location_line(block, allow_fallback=not frames)
        if location is not None and (not frames or location.start() > frames[-1].start()):
            # 既定のトレースバック形式では最終エントリーの位置行がフレーム行より後に現れ、
            # 実際に例外が発生した位置を指す。フレーム行を持たない失敗ではこの行が唯一の
            # 情報源となり、持つ場合も中間エントリーのフレーム行より正確である。
            if _is_unlisted_child_block(known_tests, after_captured=after_captured, test_name=test_name):
                continue
            file_path = pyfltr.paths.to_cwd_relative(location.group("file"))
            raw_message = _pytest_block_message(block, error_re, location)
            results.append(
                ErrorLocation(
                    file=file_path,
                    line=int(location.group("line")),
                    col=None,
                    command="pytest",
                    message=_pytest_join_message(test_name, raw_message),
                )
            )
            _consume_summary_test(summary, consumed, test_name, file_path)
            continue

        if frames:
            if _is_unlisted_child_block(known_tests, after_captured=after_captured, test_name=test_name):
                continue
            # フレーム群から最後のプロジェクト内フレームを選択
            chosen = frames[-1]  # フォールバック: 最後のフレーム
            for frame in reversed(frames):
                if _is_project_path(pyfltr.paths.to_cwd_relative(frame.group("file"))):
                    chosen = frame
                    break
            # 例外の連鎖では複数のエントリーが並ぶ。選んだフレームより後にある最初のエラー行が
            # 当該エントリーの例外であり、集計行のメッセージとも一致する。ブロック先頭から
            # 探すと内側の例外を報告する。
            error_match = error_re.search(block, chosen.end()) or error_re.search(block)
            raw_message = error_match.group("message").strip() if error_match else ""
            message = _pytest_join_message(test_name, raw_message)
            file_path = pyfltr.paths.to_cwd_relative(chosen.group("file"))
            results.append(
                ErrorLocation(file=file_path, line=int(chosen.group("line")), col=None, command="pytest", message=message)
            )
            _consume_summary_test(summary, consumed, test_name, file_path)
            continue

        # フレーム行が無いブロック: xdistワーカークラッシュを想定して専用行を探す。
        crash_match = _PYTEST_CRASH_RE.search(block)
        if crash_match is None:
            continue
        file_path = pyfltr.paths.to_cwd_relative(crash_match.group("file"))
        crashed_test = crash_match.group("test")
        if _is_unlisted_child_block(known_tests, after_captured=after_captured, test_name=crashed_test.replace("::", ".")):
            continue
        results.append(
            ErrorLocation(
                file=file_path,
                line=0,
                col=None,
                command="pytest",
                message=(
                    f"{crashed_test}: worker '{crash_match.group('worker')}' crashed while running {file_path}::{crashed_test}"
                ),
            )
        )
        # クラッシュ行のテスト名はnodeid表記（`TestX::test_y`）のため、summaryキーと同じ
        # `.`区切りへ正規化してから突合する（正規化しないとクラスベーステストで突合が失敗し
        # summary残余補完で同一テストの診断が二重生成される）。
        _consume_summary_test(summary, consumed, crashed_test.replace("::", "."), file_path)

    results.extend(_parse_pytest_from_summary(summary, consumed))
    return results


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
    "vitest": _parse_vitest_json,
    "glab-ci-lint": _parse_glab_ci_lint,
    "designmd": _parse_designmd_json,
    "lychee": _parse_lychee_json,
    "semgrep": _parse_semgrep_json,
    "bandit": _parse_bandit_json,
    "sqlfluff": _parse_sqlfluff_json,
    "uv-audit": _parse_uv_audit,
    "pnpm-audit": _parse_pnpm_audit_json,
    "npm-audit": _parse_npm_audit_json,
    "yarn-audit": _parse_yarn_audit_jsonl,
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
    """Pytest出力末尾のサマリー行を = パディング除去して抽出する。"""
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
    名前付きグループ`severity`を含む場合は`_normalize_severity`で正規化した値を
    `ErrorLocation.severity`へ格納する（biome `::notice`→`"info"`等）。
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
        severity = _normalize_severity(groups.get("severity"))
        results.append(
            ErrorLocation(
                file=pyfltr.paths.to_cwd_relative(file_path),
                line=line_num,
                col=col_num,
                command=command,
                message=message.strip(),
                rule=rule,
                severity=severity,
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
