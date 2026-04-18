"""パスユーティリティー。

パス文字列の変換・正規化に関するヘルパーを提供する。
"""

import pathlib


def normalize_separators(path: str | pathlib.Path) -> str:
    r"""相対パス前提のWindows区切り`\\`をUnix区切り`/`へ統一するヘルパー。絶対パス・正規化は扱わない。"""
    return str(path).replace("\\", "/")


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
