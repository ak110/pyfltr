"""パターンコンパイル。

ripgrep流儀のオプション体系（`-F`/`-i`/`-S`/`-w`/`-x`/`--multiline`）を
Python標準ライブラリ`re`にマッピングする。
複数パターン受理（`-e`/`-f`）はalternation（`|`）で結合する。
"""

import pathlib
import re


def compile_pattern(
    patterns: list[str],
    *,
    fixed_strings: bool,
    ignore_case: bool,
    smart_case: bool,
    word_regexp: bool,
    line_regexp: bool,
    multiline: bool,
) -> re.Pattern[str]:
    """パターン群をrepatternへコンパイルする。

    複数パターンは`|`で結合し、各パターンを非キャプチャグループ`(?:...)`で囲む。
    `fixed_strings=True`の場合は各パターンに`re.escape`を適用する。
    `smart_case=True`は`ignore_case=False`時のみ作用し、結合後パターンが
    大文字を含まない場合に限り大文字小文字を無視する（ripgrep流儀）。
    `line_regexp=True`はパターン全体を`^...$`で囲み、`$`が改行を含めない
    挙動（`re.MULTILINE`時の標準挙動と等価）に揃える。
    `multiline=True`時は`re.DOTALL | re.MULTILINE`を有効化する。

    Raises:
        ValueError: 結合後のパターンが`re.compile`で受け付けられない場合
    """
    if not patterns:
        raise ValueError("パターンが指定されていません")
    prepared = [re.escape(p) for p in patterns] if fixed_strings else list(patterns)
    # 各パターンを`(?:...)`で囲んでから`|`連結する。素のまま連結すると
    # 末端の`$`が次パターンへ波及する事故が起きるため非キャプチャグループで隔離する
    combined = "|".join(f"(?:{p})" for p in prepared)
    if word_regexp:
        combined = rf"\b(?:{combined})\b"
    if line_regexp:
        combined = rf"(?:^(?:{combined})$)"
    flags = 0
    effective_ignore_case = ignore_case
    if smart_case and not ignore_case:
        # smart-caseは結合済みパターンに大文字が含まれない場合のみignore_caseを有効化する。
        # `re.escape`で導入されるバックスラッシュ後の英字（`\d`等）は素のメタ文字に
        # 含まれず大文字判定では現れないため、結合後の文字列をそのまま判定して問題ない
        effective_ignore_case = not any(ch.isupper() for ch in combined)
    if effective_ignore_case:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL | re.MULTILINE
    elif line_regexp:
        # line_regexpは複数行ファイルに対して`^`/`$`を行単位で評価する必要があるため
        # MULTILINEを有効化する（multiline指定時は既に有効）
        flags |= re.MULTILINE
    try:
        return re.compile(combined, flags)
    except re.error as exc:
        # `re.error.pos`はパターン中の位置（0-origin）を保持する。位置情報を併記すると
        # 利用者がどの箇所を直すべきかを特定しやすい。
        pos_text = f"（位置 {exc.pos}）" if exc.pos is not None else ""
        raise ValueError(f"正規表現のコンパイルに失敗しました{pos_text}: {exc}") from exc


def read_pattern_file(path: pathlib.Path) -> list[str]:
    r"""`-f`相当のパターンファイルを読み込む。

    各行を1パターンとして扱う。空行は除外する。改行コードは`\\r\\n`/`\\n`/`\\r`の
    いずれにも対応する（`splitlines`の標準挙動）。
    """
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line]
