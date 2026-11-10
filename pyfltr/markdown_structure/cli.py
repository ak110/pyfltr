"""markdown-structureのCLI本体。

pyfltrのdispatcherから ``python -m pyfltr.markdown_structure [--fix] <files>`` として起動される。
dispatcherが対象ファイルをフィルタして渡すため、本CLIはディレクトリ展開や除外判定を行わない。
診断は`<ファイル>:<行>:<列>: error: <文面>`の形で標準出力へ書き、ファイルパスは
`pyfltr.paths.normalize_separators`で区切りを`/`へ統一する（解析を経ずに生出力が公開される経路があるため）。
"""

import argparse
import bisect
import pathlib
import re
import sys

import pyfltr.paths
from pyfltr.markdown_structure import check


def main() -> int:
    """指定ファイルを検査し、成功0・違反1・入力エラー2を返す。"""
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
            print(f"{path}: 入力エラー: {exc}。UTF-8の読み書きが可能なファイルを指定してください", file=sys.stderr)
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
    # 後続入力の失敗で一部だけが修正されないよう、全入力の解析と出力の生成を先に終える。
    if args.fix and result == 2:
        return result
    for path, content in changes.items():
        try:
            path.write_bytes(content)
        except OSError as exc:
            print(f"{path}: 入力エラー: {exc}。UTF-8の読み書きが可能なファイルを指定してください", file=sys.stderr)
            result = 2
    for path, text, issues in prepared:
        starts = [0, *(m.end() for m in re.finditer("\n", text))]
        for issue in issues:
            line = bisect.bisect_right(starts, issue.offset)
            column = issue.offset - starts[line - 1] + 1
            print(f"{pyfltr.paths.normalize_separators(str(path))}:{line}:{column}: error: {issue.message}")
    return result
