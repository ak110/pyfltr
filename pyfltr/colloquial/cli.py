import argparse
import pathlib
import sys

import pyfltr.paths
from pyfltr.colloquial import check as colloquial_check

_EXCERPT_LIMIT = 100
_NO_REPLACEMENT_GUIDANCE = "文意を保ったまま書き言葉の表現へ言い換える"


def main() -> int:
    parser = argparse.ArgumentParser(description="口語的な日本語表現を検出する。")
    parser.add_argument("paths", nargs="+", type=pathlib.Path, help="検査対象ファイル")
    args = parser.parse_args()

    deny_patterns = colloquial_check.load_patterns(colloquial_check.DENY_PATH, kanji_left_boundary=True)
    allow_patterns = colloquial_check.load_patterns(colloquial_check.ALLOW_PATH)
    if not deny_patterns:
        return 0

    total = 0
    for path in args.paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            location = pyfltr.paths.normalize_separators(path)
            print(
                f"{location}: 読み込めないため口語表現の検査をスキップしました: {e}。"
                "UTF-8で保存されていることと読み取り権限を確認してください",
                file=sys.stderr,
            )
            continue
        for hit in colloquial_check.scan_text(text, deny_patterns, allow_patterns):
            print(_format_hit(path, hit))
            total += 1
    return 1 if total else 0


def _format_hit(path: pathlib.Path, hit: tuple[int, int, str, str, str | None]) -> str:
    line_no, col, match_str, snippet, replacement = hit
    excerpt = snippet if len(snippet) <= _EXCERPT_LIMIT else snippet[:_EXCERPT_LIMIT] + "…"
    suggestion = f" -> [{replacement}]" if replacement else f" -> ({_NO_REPLACEMENT_GUIDANCE})"
    location = pyfltr.paths.normalize_separators(path)
    return f"{location}:{line_no}:{col}: [{match_str}]{suggestion} {excerpt}"
