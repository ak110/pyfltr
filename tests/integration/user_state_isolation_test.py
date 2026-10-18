"""ユーザー単位の保存先が全試験から既定で隔離されることの構成検証。

pyfltrはユーザー単位の保存先（実行アーカイブ・結果キャッシュ・置換履歴の共通ルートとグローバル設定）を持つ。
`tests/conftest.py`のautouseフィクスチャが上書き用の環境変数を試験用一時領域へ固定するため、
隔離用のフィクスチャを要求しない試験でも、保存先の解決関数は開発者の実環境を指さない。
既定の隔離が外れると、本試験は保存先が`basetemp`の外を指すため失敗する。
保存先を新設する場合は、その解決関数を本試験の対象へ加える。
"""

import pathlib

import pytest

import pyfltr.config.model
import pyfltr.grep_.history
import pyfltr.state.archive


def test_user_state_paths_are_isolated_by_default(tmp_path_factory: pytest.TempPathFactory) -> None:
    basetemp = tmp_path_factory.getbasetemp().resolve()
    resolved: dict[str, pathlib.Path] = {
        "cache_root": pyfltr.state.archive.default_cache_root(),
        "replace_history": pyfltr.grep_.history.default_history_root(),
        "global_config": pyfltr.config.model.default_global_config_path(),
    }
    outside = {name: path for name, path in resolved.items() if not path.resolve().is_relative_to(basetemp)}
    assert not outside, f"試験用一時領域（{basetemp}）の外を指す保存先がある: {outside}"
