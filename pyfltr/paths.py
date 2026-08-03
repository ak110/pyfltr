"""パスユーティリティー。

パス文字列の変換・正規化に関するヘルパーを提供する。
"""

import pathlib


def normalize_separators(path: str | pathlib.Path) -> str:
    r"""Windows区切り`\\`をUnix区切り`/`へ統一する。絶対パスと相対パスの双方を扱う。"""
    return str(path).replace("\\", "/")


def sanitize_command_name(name: str) -> str:
    """コマンド名をファイルシステム安全な形式へ変換する。

    アーカイブ保存キー（`archive.py`の`tools/<sanitize(command)>/`配下）と
    JSONL`command.truncated.archive`参照パス（`llm_output.py`）の双方で共通利用する。
    カスタムコマンド側でスラッシュ等が入る可能性があるため最低限のサニタイズを行う。
    英数字・ハイフン・アンダースコア以外は`_`へ置換し、空文字になった場合は`_`を返す。
    """
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    return safe or "_"


def to_cwd_relative(path: str | pathlib.Path) -> str:
    """パスをcwd基準の相対パスへ変換する。

    区切り文字は全分岐で`/`へ統一する。cwd配下の絶対パスはcwd基準の相対パスへ、
    相対パスはそのままの構成で返す。cwd配下でない絶対パスは相対化できないため
    絶対パスのまま返すが、区切り文字は同様に`/`へ統一する。
    表現を揃えるのは、返り値がプロジェクト内外の判定・診断の突合キー・
    利用者向け出力のいずれにも用いられ、経路ごとの表現差が判定の誤りを招くためである。
    """
    as_path = pathlib.Path(path)
    if as_path.is_absolute():
        try:
            return normalize_separators(as_path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            return normalize_separators(path)
    return normalize_separators(path)
