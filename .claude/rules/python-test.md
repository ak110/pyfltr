---
paths:
  - "**/*_test.py"
  - "**/conftest.py"
---

# Pythonテストコード記述スタイル

- テストコードは`pytest`で書く
- 網羅性のため、必要に応じて`@pytest.mark.parametrize`を使用する
- テスト関数内で使用しないfixture（副作用のみが必要な場合）は
  `@pytest.mark.usefixtures("fixture_name")` を使用する
  - `@pytest.mark.parametrize(..., indirect=True)` との併用も可
  - デコレーター順序（外側から内側）:
    `parametrize` → `asyncio` → `usefixtures`

## Fixture のコーディングルール

- 関数名: `_`で始める、テストから参照する場合は`name`で別名指定
- scope: 可能な限り広いスコープ（session > package > module > function）
- autouse: モジュール単位は積極的に使用、package/session単位は副作用に注意
- 型ヒント: 必須、複数値返す場合は型エイリアスを定義
