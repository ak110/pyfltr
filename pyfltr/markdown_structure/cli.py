import argparse
import bisect
import pathlib
import re
import sys

import pyfltr.paths
from pyfltr.markdown_structure import check


def _report_input_error(path: pathlib.Path, exc: Exception) -> None:
    location = pyfltr.paths.normalize_separators(str(path))
    print(f"{location}:1:1: error: 入力エラー: {exc}。UTF-8の読み書きが可能なファイルを指定してください", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Markdownの1文1行と全角丸括弧の入れ子を検査する。")
    parser.add_argument("--fix", action="store_true", help="文境界の改行とリスト継続字下げを追加する")
    parser.add_argument("paths", nargs="*", type=pathlib.Path, help="検査対象ファイル")
    args = parser.parse_args()
    result = 0
    prepared: list[tuple[pathlib.Path, str, list[check.Issue]]] = []
    changes: dict[pathlib.Path, bytes] = {}
    for path in args.paths:
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeError) as exc:
            _report_input_error(path, exc)
            result = 2
            continue
        issues, insertions = check.inspect(text)
        if args.fix and insertions:
            newline = "\r\n" if "\r\n" in text else "\n"
            for position, indent in sorted(insertions.items(), reverse=True):
                text = text[:position] + newline + indent + text[position:]
            changes[path] = text.encode("utf-8")
            issues, _ = check.inspect(text)
        prepared.append((path, text, issues))
        result = max(result, int(bool(issues)))
    if args.fix and result == 2:
        return result
    for path, content in changes.items():
        try:
            path.write_bytes(content)
        except OSError as exc:
            _report_input_error(path, exc)
            result = 2
    for path, text, issues in prepared:
        starts = [0, *(m.end() for m in re.finditer("\n", text))]
        for issue in issues:
            line = bisect.bisect_right(starts, issue.offset)
            column = issue.offset - starts[line - 1] + 1
            print(f"{pyfltr.paths.normalize_separators(str(path))}:{line}:{column}: error: {issue.message}")
    return result
