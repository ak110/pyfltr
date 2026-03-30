---
paths:
  - "**/*_test.py"
  - "**/conftest.py"
---

# Pythonテストコード記述スタイル

- テストコードは`pytest`で書く
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- 網羅性のため、必要に応じて`@pytest.mark.parametrize`を使用する
- テスト関数内で使用しない fixture（副作用のみが必要な場合）は `@pytest.mark.usefixtures("fixture_name")` を使用する
  - `@pytest.mark.parametrize(..., indirect=True)` との併用も可
  - デコレーター順序: `@pytest.mark.parametrize` → `@pytest.mark.asyncio` → `@pytest.mark.usefixtures(...)` （外側から内側）

## Fixture のコーディングルール

- 関数名: `_`で始める、テストから参照する場合は`name`で別名指定
- scope: 可能な限り広いスコープ（session > package > module > function）
- autouse: モジュール単位は積極的に使用、package/session単位は副作用に注意
- 型ヒント: 必須、複数値返す場合は型エイリアスを定義

## テストコードの例

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

- テストコードの実行は `uv run pyfltr <path>` を使う (pytestを直接呼び出さない)
  - `-vv`などが必要な場合に限り `uv run pyfltr -vv <path>` のようにする
