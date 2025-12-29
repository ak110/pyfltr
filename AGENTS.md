# カスタム指示

## 原則

- 以下を念頭に置いて実装を進めること
  - DRY: **Don't Repeat Yourself**
  - KISS: **Keep It Simple, Stupid**
  - SSOT: **Single Source Of Truth**
  - SRP: **Single Responsibility Principle**
  - コードには How、
    テストコードには What、
    コミットログには Why、
    コードコメントには Why not を書く。

## コーディングスタイル

- importは可能な限り`import xxx`形式で書く (`from xxx import yyy`ではなく)
- タイプヒントは可能な限り書く
  - `typing.List`ではなく`list`を使用する。`dict`やその他も同様。
  - `typing.Optional`ではなく`| None`を使用する。
- docstringは基本的には概要のみ書く
- ログは`logging`を使う
- 日付関連の処理は`datetime`を使う
- ファイル関連の処理は`pathlib`を使う
- テーブルデータの処理には`polars`を使う (`pandas`は使わない)
- モジュール追加時は`README.md`も更新する
- コードを書いた後は必ず`make test`する。コードフォーマット、mypy, pytestなどがまとめて実行される。
― 新しいファイルを作成する場合は近い階層の代表的なファイルを確認し、可能な限りスタイルを揃える
- `git grep`コマンドを活用して影響範囲やコードスタイルを調査する
- 関数やクラスなどの定義の順番は可能な限りトップダウンにする。
  つまり関数Aから関数Bを呼び出す場合、関数Aを前に、関数Bを後ろに定義する。

## テストコード

- テストコードは`pytest`で書く
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- テストコードは速度と簡潔さを重視する。
  - テスト関数を細かく分けず、一連の流れをまとめて1つの関数にする。
  - 網羅性のため、必要に応じて `pytest.mark.parametrize` を使用する。

テストコードの例:

```python
"""テストコード。"""

import pathlib

import pytest
import pyfltr.xxx_


@pytest.mark.parametrize(
    "x,expected",
    [
        ("test1", "test1"),
        ("test2", "test2"),
    ],
)
def test_yyy(tmp_path: pathlib.Path, x: str, expected: str) -> None:
    """yyyのテスト。"""
    actual = pyfltr.xxx_.yyy(tmp_path, x)
    assert actual == expected

```

- テストコードを書いたら `uv run pytest` でテストを実行する

## リリース手順

- DEVELOPMENT.mdを参照
