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


@dataclasses.dataclass
class ErrorLocation:
    """エラー箇所の情報。"""

    file: str
    line: int
    col: int | None
    command: str
    message: str


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


def format_error(error: ErrorLocation) -> str:
    """エラー箇所を表示用文字列にフォーマットする。"""
    col_str = f":{error.col}" if error.col else ""
    return f"{error.file}:{error.line}{col_str}: [{error.command}] {error.message}"


# ビルトインパーサー用の正規表現パターン
# 各パターンはfile, line, messageの名前付きグループが必須。colは任意。
_BUILTIN_PATTERNS: dict[str, str] = {
    # mypy出力例: src/foo.py:10: error: xxx [error-code]
    "mypy": r"(?P<file>[^\s:]+):(?P<line>\d+):\s*error:\s*(?P<message>.+)",
    # pylint出力例: src/foo.py:10:5: C0114: xxx
    "pylint": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[CRWEF]\d+:.+)",
    # ruff check出力例: src/foo.py:10:5: E001 xxx
    "ruff-check": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>[A-Z]+\d+\s+.+)",
    # pyright出力例: src/foo.py:10:5 - error: xxx
    "pyright": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+)\s*-\s*error:\s*(?P<message>.+)",
    # ty check --output-format concise 出力例: src/foo.py:10:5: error[rule-name] Message text
    "ty": r"(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>(?:error|warning)\[.+?\]\s+.+)",
    # markdownlint-cli2出力例: file.md:3 MD001/heading-increment Heading levels ...
    "markdownlint": r"(?P<file>[^\s:]+):(?P<line>\d+)\s+(?P<message>MD\d+\S*\s+.+)",
    # textlint --format compact出力例: /path/file.md: line 1, col 1, Error - message (rule)
    "textlint": r"(?P<file>[^\s:]+):\s*line\s+(?P<line>\d+),\s*col\s+(?P<col>\d+),\s*\w+\s*-\s*(?P<message>.+)",
    # pytest出力例: FAILED tests/xxx_test.py::test_yyy - AssertionError
    "pytest": r"FAILED\s+(?P<file>[^\s:]+)::(?P<message>\S+)",
    # biome --reporter=github 出力例 (実機確認済み、line と col の間に endLine が挟まる):
    # ::error title=lint/suspicious/noDoubleEquals,file=src/foo.ts,line=1,endLine=1,col=7,endColumn=9::Use === instead of ==
    # [^:]*? で順序非依存かつ `::` 終端を跨がないようマッチする。
    "biome": (
        r"::(?:error|warning)\s+[^:]*?file=(?P<file>[^,]+)"
        r"[^:]*?line=(?P<line>\d+)"
        r"[^:]*?col=(?P<col>\d+)"
        r"[^:]*?::(?P<message>.+)"
    ),
}


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
    output = output.strip()
    if not output:
        return []
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return []
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
            full_message = f"{text} ({rule_id})" if rule_id else text
            results.append(
                ErrorLocation(
                    file=_normalize_path(file_path),
                    line=line,
                    col=col,
                    command="eslint",
                    message=full_message.strip(),
                )
            )
    return results


# コマンド名 -> 関数ベースパーサー。regex で扱いにくい出力 (JSON など) に使う。
_CUSTOM_PARSERS: dict[str, typing.Callable[[str], list[ErrorLocation]]] = {
    "eslint": _parse_eslint_json,
}


def _parse_with_pattern(command: str, output: str, pattern: str) -> list[ErrorLocation]:
    """正規表現パターンでエラー箇所をパースする。"""
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
        message = groups.get("message", "")
        try:
            line_num = int(line_str)
        except ValueError:
            continue
        col_num: int | None = None
        if col_str is not None:
            with contextlib.suppress(ValueError):
                col_num = int(col_str)
        results.append(
            ErrorLocation(
                file=_normalize_path(file_path),
                line=line_num,
                col=col_num,
                command=command,
                message=message.strip(),
            )
        )
    return results


def _normalize_path(file_path: str) -> str:
    """パスをcwd基準の相対パスに正規化する。"""
    path = pathlib.Path(file_path)
    if path.is_absolute():
        try:
            return str(path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            # cwdの配下でない場合はそのまま返す
            return file_path
    return file_path
