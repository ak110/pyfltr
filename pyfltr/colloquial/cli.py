"""口語表現チェッカーのCLI本体。

pyfltrのdispatcherから ``python -m pyfltr.colloquial <files>`` として起動される。
dispatcherが対象ファイルをフィルタして渡すため、
本CLIはディレクトリ展開や除外判定を行わずファイルパスを直接受け取る。

診断行のファイルパスは`pyfltr.paths.normalize_separators`を経由して区切りを`/`へ統一する。
本CLIの標準出力は`pyfltr.parsing.entry`の解析を経て正規化される経路のほかに、
`show-run --commands colloquial-check --output`とMCPツール応答へ生のまま載る経路を持つ。
後者は解析を経ないため、生成時点での正規化が公開するファイル位置の表現の契約
（`docs/development/architecture.md`）を満たす唯一の手段となる。
"""

import argparse
import pathlib
import sys

import pyfltr.paths
from pyfltr.colloquial import check as colloquial_check

_EXCERPT_LIMIT = 100
_NO_REPLACEMENT_GUIDANCE = "文意を保ったまま書き言葉の表現へ言い換える"


def main() -> int:
    """検査対象ファイルを読み込み、検出結果をstdoutへ出力する。

    戻り値は検出0件のとき0、1件以上のとき1（pyfltrのlinter終了コード規約に従う）。
    """
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
            # 検査できなかったことを伏せると検出0件と区別できないため、原因と確認事項を通知する。
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
    # 辞書に置換候補が無い表現は、言い換えの方針を候補の位置へ示す。
    suggestion = f" -> [{replacement}]" if replacement else f" -> ({_NO_REPLACEMENT_GUIDANCE})"
    location = pyfltr.paths.normalize_separators(path)
    return f"{location}:{line_no}:{col}: [{match_str}]{suggestion} {excerpt}"
