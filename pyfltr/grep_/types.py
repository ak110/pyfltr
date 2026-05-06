"""grep / replace共通のデータクラス定義。

CLI層・JSONL層・MCP層が同じ構造体を共有することで、レコード変換の二重管理を避ける。
すべて不変（`frozen=True`）として扱い、生成元のスキャナーや置換器は
副作用を内側に閉じ込める。
"""

import dataclasses
import pathlib


@dataclasses.dataclass(frozen=True)
class MatchRecord:
    """grepの1マッチを表す。

    Attributes:
        file: マッチを検出したファイルパス
        line: マッチ開始行番号（1-origin）
        col: マッチ開始列番号（1-origin、文字単位）
        end_col: マッチ終了列番号（1-origin、`None`は単一行マッチでなく未確定）
        line_text: マッチを含む行の本文（改行を除く）
        match_text: マッチした文字列そのもの
        before_lines: `-B`コンテキストの前行群（行番号順、改行を除く）
        after_lines: `-A`コンテキストの後行群（行番号順、改行を除く）
    """

    file: pathlib.Path
    line: int
    col: int
    end_col: int | None
    line_text: str
    match_text: str
    before_lines: list[str]
    after_lines: list[str]


@dataclasses.dataclass(frozen=True)
class FileMatchSummary:
    """ファイル単位のマッチ集計。

    `--files-with-matches`/`--count`等の出力種別で個別マッチを抑え、
    ファイル単位の件数だけを返す経路で使う。
    """

    file: pathlib.Path
    count: int
    lines: list[int]


@dataclasses.dataclass(frozen=True)
class ReplaceRecord:
    """replaceの1置換箇所を表す。

    Attributes:
        file: 置換を実施したファイルパス
        line: 置換対象行の行番号（1-origin、置換前テキストの行番号）
        col: 置換開始列番号（1-origin、文字単位）
        before_line: 置換前の行本文（改行を除く）
        after_line: 置換後の行本文（改行を除く）
        before_text: 置換前のマッチ部分
        after_text: 置換後の文字列
    """

    file: pathlib.Path
    line: int
    col: int
    before_line: str
    after_line: str
    before_text: str
    after_text: str


@dataclasses.dataclass(frozen=True)
class FileChangeSummary:
    """replace適用結果のファイル単位サマリー。"""

    file: pathlib.Path
    count: int
    before_hash: str | None
    after_hash: str | None


@dataclasses.dataclass(frozen=True)
class ReplaceCommandMeta:
    """replace実行時のコマンドメタ情報。

    `replace_id`は実書き込み時にULIDで採番し、dry-run時は`None`にする。
    保存対象には正規表現と置換式の原文を含めるため、再現実行時の再構築に利用できる。
    """

    replace_id: str | None
    dry_run: bool
    fixed_strings: bool
    pattern: str
    replacement: str
    encoding: str
