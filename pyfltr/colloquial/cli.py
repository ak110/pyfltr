"""口語表現チェッカーのCLI本体。

pyfltrのdispatcherから ``python -m pyfltr.colloquial <files>`` として起動される。
dispatcherが対象ファイルをフィルタして渡すため、
本CLIはディレクトリ展開や除外判定を行わずファイルパスを直接受け取る。
"""

import argparse
import pathlib

from pyfltr.colloquial import check as colloquial_check

_EXCERPT_LIMIT = 100


def main() -> int:
    """検査対象ファイルを読み込み、検出結果をstdoutへ出力する。

    戻り値は検出0件のとき0、1件以上のとき1（pyfltrのlinter終了コード規約に従う）。
    """
    parser = argparse.ArgumentParser(description="口語的な日本語表現を検出する。")
    parser.add_argument("paths", nargs="+", type=pathlib.Path, help="検査対象ファイル")
    args = parser.parse_args()

    deny_patterns = colloquial_check.load_patterns(colloquial_check.DENY_PATH)
    allow_patterns = colloquial_check.load_patterns(colloquial_check.ALLOW_PATH)
    if not deny_patterns:
        return 0

    total = 0
    for path in args.paths:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for hit in colloquial_check.scan_text(text, deny_patterns, allow_patterns):
            print(_format_hit(path, hit))
            total += 1
    return 1 if total else 0


def _format_hit(path: pathlib.Path, hit: tuple[int, int, str, str, str | None]) -> str:
    line_no, col, match_str, snippet, replacement = hit
    excerpt = snippet if len(snippet) <= _EXCERPT_LIMIT else snippet[:_EXCERPT_LIMIT] + "…"
    suggestion = f" -> [{replacement}]" if replacement else ""
    return f"{path}:{line_no}:{col}: [{match_str}]{suggestion} {excerpt}"
