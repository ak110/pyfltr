"""ユーザー単位のキャッシュ保存先。

実行アーカイブ・結果キャッシュ・置換履歴・実行枠のロックが共有するルートを解決する。
層構成（`tests/integration/layers_test.py`）の全層から参照できるよう最上位に置く。
"""

import os
import pathlib

import platformdirs


def default_cache_root() -> pathlib.Path:
    r"""XDG 準拠のキャッシュルートを返す。

    Linuxでは`~/.cache/pyfltr/`、macOSでは`~/Library/Caches/pyfltr/`、
    Windowsでは`%LOCALAPPDATA%\pyfltr\Cache`になる。環境変数
    `PYFLTR_CACHE_DIR`が設定されていればそれを優先する（テストや運用上の
    強制上書き用）。

    `appauthor=False`を渡すのは、未指定時にWindowsで`appname`が
    appauthorとしても付与され`%LOCALAPPDATA%\pyfltr\pyfltr\Cache`に
    なる挙動を回避するため。

    プロジェクトローカル（`.pyfltr_cache/`のようなリポジトリ内ディレクトリ）
    には保存しない方針を採用する。`.gitignore`運用の負担を増やさず、
    複数プロジェクト横断での参照を可能にするため。
    """
    override = os.environ.get("PYFLTR_CACHE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(platformdirs.user_cache_dir("pyfltr", appauthor=False))
