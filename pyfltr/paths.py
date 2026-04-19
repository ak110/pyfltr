"""パスユーティリティー。

パス文字列の変換・正規化に関するヘルパーを提供する。
"""

import pathlib


def normalize_separators(path: str | pathlib.Path) -> str:
    r"""相対パス前提のWindows区切り`\\`をUnix区切り`/`へ統一するヘルパー。絶対パス・正規化は扱わない。"""
    return str(path).replace("\\", "/")


def sanitize_command_name(name: str) -> str:
    """コマンド名をファイルシステム安全な形式へ変換する。

    アーカイブ保存キー（``archive.py``の``tools/<sanitize(command)>/``配下）と
    JSONL``command.truncated.archive``参照パス（``llm_output.py``）の双方で共通利用する。
    カスタムコマンド側でスラッシュ等が入る可能性があるため最低限のサニタイズを行う。
    英数字・ハイフン・アンダースコア以外は``_``へ置換し、空文字になった場合は``_``を返す。
    """
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    return safe or "_"


def to_cwd_relative(path: str | pathlib.Path) -> str:
    """パスをcwd基準の相対パスに正規化する。区切り文字はスラッシュに統一する。

    絶対パスがcwd配下にあるときは相対パスへ変換する。cwd配下でなければ入力文字列を
    そのまま返す（文字列化と区切り文字正規化のみ）。相対パスは``normalize_separators``
    と同じ扱いで、区切り文字のみ``/``へ統一する。
    """
    as_path = pathlib.Path(path)
    if as_path.is_absolute():
        try:
            result = str(as_path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            return str(path)
        return normalize_separators(result)
    return normalize_separators(path)
