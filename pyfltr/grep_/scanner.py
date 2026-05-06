"""ファイル走査とマッチ抽出。

`expand_all_files()`で展開済みのファイル群に対して、`re.Pattern`を適用して
`MatchRecord`/`FileMatchSummary`を逐次生成する。
前後コンテキスト（`-A`/`-B`/`-C`）の重複統合とper-file/全体件数上限の打ち切り、
ファイルタイプ・globフィルタ、エンコーディングデコードエラー時のスキップを担う。
"""

import collections.abc
import pathlib
import re

import pyfltr.warnings_
from pyfltr.grep_.types import FileMatchSummary, MatchRecord

# ripgrep流儀の言語タイプマッピング（最低限の9種）。
# キーがタイプ名、値がglobパターン群。`Path.match`が末尾セグメントのglob一致を判定するため、
# 拡張子のみで`*.py`形式に揃える。
_TYPE_PATTERNS: dict[str, tuple[str, ...]] = {
    "python": ("*.py", "*.pyi"),
    "rust": ("*.rs",),
    "ts": ("*.ts", "*.tsx"),
    "js": ("*.js", "*.jsx", "*.mjs", "*.cjs"),
    "md": ("*.md", "*.markdown"),
    "json": ("*.json",),
    "toml": ("*.toml",),
    "yaml": ("*.yaml", "*.yml"),
    "shell": ("*.sh", "*.bash", "*.zsh"),
}


def filter_files_by_type(files: list[pathlib.Path], types: list[str]) -> list[pathlib.Path]:
    """`--type`相当のファイルタイプフィルタ。

    `types`が空ならフィルタを行わず入力をそのまま返す。
    未知のタイプ名は無視する（マッチ件数0扱い）。
    """
    if not types:
        return list(files)
    patterns: list[str] = []
    for type_name in types:
        patterns.extend(_TYPE_PATTERNS.get(type_name, ()))
    if not patterns:
        return []
    return [f for f in files if any(f.match(p) for p in patterns)]


def filter_by_globs(files: list[pathlib.Path], globs: list[str]) -> list[pathlib.Path]:
    """`-g/--glob`相当のglobフィルタ。

    `globs`が空なら入力をそのまま返す。
    複数glob指定時はOR結合（いずれかに一致したファイルを残す）。
    """
    if not globs:
        return list(files)
    return [f for f in files if any(f.match(g) for g in globs)]


def scan_files(
    files: list[pathlib.Path],
    pattern: re.Pattern[str],
    *,
    before_context: int,
    after_context: int,
    max_per_file: int,
    max_total: int,
    encoding: str,
    max_filesize: int | None,
    multiline: bool,
) -> collections.abc.Iterator[MatchRecord | FileMatchSummary]:
    """ファイル群をスキャンしてマッチを順次生成する。

    Args:
        files: 走査対象ファイル群（`expand_all_files()`等で除外フィルタ済みを想定）
        pattern: `compile_pattern()`で生成済みの`re.Pattern`
        before_context: マッチの前に出力する行数（`-B`、0以上）
        after_context: マッチの後に出力する行数（`-A`、0以上）
        max_per_file: ファイルごとの最大マッチ件数（0以下で無制限）
        max_total: 全体の最大マッチ件数（0以下で無制限）
        encoding: ファイル読み込み時のエンコーディング
        max_filesize: 走査対象ファイルサイズ上限（バイト、`None`で無制限）
        multiline: ファイル全体に対して`finditer`を適用するか（行単位ではなく）

    Yields:
        `MatchRecord`を逐次生成する。本実装では`FileMatchSummary`は生成しないが、
        ファイル単位サマリー出力経路（`--count`等）の拡張余地として戻り値型に含める

    エンコーディングデコードエラーが発生したファイルはスキップし、
    `pyfltr.warnings_`へ警告を蓄積する。
    """
    total = 0
    for file in files:
        if 0 < max_total <= total:
            return
        if max_filesize is not None and max_filesize > 0:
            try:
                if file.stat().st_size > max_filesize:
                    continue
            except OSError:
                continue
        try:
            text = file.read_text(encoding=encoding)
        except UnicodeDecodeError:
            pyfltr.warnings_.emit_warning(
                source="grep",
                message=f"エンコーディングエラーのためスキップしました: {file}",
            )
            continue
        except OSError:
            pyfltr.warnings_.emit_warning(
                source="grep",
                message=f"ファイル読み込みに失敗したためスキップしました: {file}",
                exc_info=True,
            )
            continue
        for per_file, record in enumerate(
            _scan_text(
                file=file,
                text=text,
                pattern=pattern,
                before_context=before_context,
                after_context=after_context,
                multiline=multiline,
            )
        ):
            if 0 < max_per_file <= per_file:
                break
            yield record
            total += 1
            if 0 < max_total <= total:
                return


def _scan_text(
    *,
    file: pathlib.Path,
    text: str,
    pattern: re.Pattern[str],
    before_context: int,
    after_context: int,
    multiline: bool,
) -> collections.abc.Iterator[MatchRecord]:
    """単一ファイルの本文に対してマッチを抽出する。

    - 行単位走査時は各行に対して`finditer`を適用し、行頭からの列番号を採用する
    - マルチラインモード時はファイル全体に`finditer`を適用し、マッチ開始位置から
      行番号を逆算する。end_colはマッチ末尾の行頭からの列番号で算出する
    - 同一ファイル内で連続するマッチの前後コンテキストは重複行を生成しないよう
      隣接マッチ起点で重複範囲を切り詰める
    """
    lines = text.splitlines()
    if not lines:
        return
    if multiline:
        yield from _scan_multiline(
            file=file,
            text=text,
            lines=lines,
            pattern=pattern,
            before_context=before_context,
            after_context=after_context,
        )
        return
    # 行単位走査。前マッチの末尾行（`after`に含まれた最終行）以降からコンテキストを切り詰める
    last_after_end_line = 0  # 直前マッチが`after`まで含めて占有した最終行番号（1-origin、0は未占有）
    for line_index, line_text in enumerate(lines):
        line_no = line_index + 1
        for m in pattern.finditer(line_text):
            start_col = m.start() + 1
            end_col = m.end() + 1
            before_start = max(0, line_index - before_context)
            # 直前マッチが既に出力した行と重ならないよう開始位置を後方へ移動する
            before_start = max(before_start, last_after_end_line)
            before_lines = lines[before_start:line_index]
            after_end = min(len(lines), line_index + 1 + after_context)
            after_lines = lines[line_index + 1 : after_end]
            yield MatchRecord(
                file=file,
                line=line_no,
                col=start_col,
                end_col=end_col,
                line_text=line_text,
                match_text=m.group(0),
                before_lines=list(before_lines),
                after_lines=list(after_lines),
            )
            # 次マッチのbefore範囲がここまでの行と重ならないよう、現マッチの`after`末尾を記録
            last_after_end_line = after_end


def _scan_multiline(
    *,
    file: pathlib.Path,
    text: str,
    lines: list[str],
    pattern: re.Pattern[str],
    before_context: int,
    after_context: int,
) -> collections.abc.Iterator[MatchRecord]:
    """マルチラインモード時のマッチ抽出。

    `finditer`をテキスト全体に適用し、マッチ開始位置から行番号・列番号を逆算する。
    """
    # 各行の開始オフセット（`text`内バイト数ではなく文字数）。
    # `splitlines`では末尾改行が除去されるため、`text`を`\n`で分割した結果と整合させる目的で
    # 単純に`\n`位置を集計する形に揃える。
    line_starts: list[int] = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)
    last_after_end_line = 0
    for m in pattern.finditer(text):
        start_pos = m.start()
        end_pos = m.end()
        line_index = _find_line_index(line_starts, start_pos)
        end_line_index = _find_line_index(line_starts, max(end_pos - 1, start_pos))
        line_no = line_index + 1
        col = start_pos - line_starts[line_index] + 1
        end_col = end_pos - line_starts[end_line_index] + 1
        line_text = lines[line_index] if line_index < len(lines) else ""
        match_text = m.group(0)
        before_start = max(0, line_index - before_context)
        before_start = max(before_start, last_after_end_line)
        before_lines = lines[before_start:line_index]
        after_end = min(len(lines), end_line_index + 1 + after_context)
        after_lines = lines[end_line_index + 1 : after_end]
        yield MatchRecord(
            file=file,
            line=line_no,
            col=col,
            end_col=end_col,
            line_text=line_text,
            match_text=match_text,
            before_lines=list(before_lines),
            after_lines=list(after_lines),
        )
        last_after_end_line = after_end


def _find_line_index(line_starts: list[int], pos: int) -> int:
    """文字オフセットから0-origin行番号を返す。"""
    # 線形探索で十分（マルチラインマッチの想定回数が低いため）。
    # 二分探索化はホットスポットになった場合に再考する
    line_index = 0
    for i, start in enumerate(line_starts):
        if start <= pos:
            line_index = i
        else:
            break
    return line_index
