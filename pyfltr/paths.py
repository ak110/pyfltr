"""パスユーティリティー。

パス文字列の変換・正規化に関するヘルパーを提供する。
"""

import os
import pathlib


def normalize_separators(path: str | pathlib.Path) -> str:
    r"""Windows区切り`\\`をUnix区切り`/`へ統一する。絶対パスと相対パスの双方を扱う。"""
    return str(path).replace("\\", "/")


def sanitize_command_name(name: str) -> str:
    """コマンド名をファイルシステム安全な形式へ変換する。

    アーカイブ保存キー（`archive.py`の`tools/<sanitize(command)>/`配下）と
    JSONL`command.truncated.archive`参照パス（`output/jsonl.py`）の双方で共通利用する。
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


def to_start_relative(
    path: str | pathlib.Path,
    *,
    path_base: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
) -> str:
    """ツール出力のパスを実行起点基準の相対パスへ変換する。

    `path_base`はツールが相対パスを出力するときの基準（ツールの実行cwd）、
    `start_cwd`はpyfltr実行の起点（`--work-dir`・MCPの`work_dir`適用後）である。
    いずれも省略時はプロセスのcwdを使う。

    相対パスは`path_base`を基準に絶対化し、`..`を字句的に畳み込む（シンボリックリンクは解決しない）。
    起点配下なら起点相対、起点外なら絶対パスを返す。区切り文字は全分岐で`/`へ統一する。
    空文字列と`<`で始まる擬似名（`<stdin>`等）はパスとして扱わず区切りの統一だけを行う。

    診断の`file`は`--only-failed`・`retry_command`で起点相対の対象集合と突合するため、
    サブプロジェクトのcwdで実行したツールの出力も本関数で同じ基準へ揃える。
    """
    text = str(path)
    if not text or text.startswith("<"):
        return normalize_separators(text)
    start = start_cwd if start_cwd is not None else pathlib.Path.cwd()
    as_path = pathlib.Path(text)
    if not as_path.is_absolute():
        as_path = (path_base if path_base is not None else start) / as_path
    normalized = pathlib.Path(os.path.normpath(as_path))
    try:
        return normalize_separators(normalized.relative_to(pathlib.Path(os.path.normpath(start))))
    except ValueError:
        return normalize_separators(normalized)
