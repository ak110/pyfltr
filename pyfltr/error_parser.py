"""エラー出力パーサー。

各コマンドの出力からエラー箇所(ファイル名:行番号)を抽出する。
ビルトインパーサーとカスタム正規表現の両方に対応。
"""

import contextlib
import dataclasses
import pathlib
import re


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

    error_patternが指定されていればそれを使用、なければビルトインパーサーを使用。
    ビルトインパーサーもなければ空リストを返す。
    """
    if error_pattern is not None:
        return _parse_with_pattern(command, output, error_pattern)
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
    # pytest出力例: FAILED tests/xxx_test.py::test_yyy - AssertionError
    "pytest": r"FAILED\s+(?P<file>[^\s:]+)::(?P<message>\S+)",
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
