"""パスユーティリティー。

パス文字列の変換・正規化に関するヘルパーを提供する。
"""

import pathlib


def normalize_separators(path: str | pathlib.Path) -> str:
    r"""相対パス前提のWindows区切り`\\`をUnix区切り`/`へ統一するヘルパー。絶対パス・正規化は扱わない。"""
    return str(path).replace("\\", "/")
