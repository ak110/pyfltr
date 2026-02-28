# スタイルガイド

## Python実装

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
- コードを書いた後は必ず`make test`する。コードフォーマット、mypy、pytestなどがまとめて実行される
- 新しいファイルを作成する場合は近い階層の代表的なファイルを確認し、可能な限りスタイルを揃える
- `git grep`コマンドを活用して影響範囲やコードスタイルを調査する
- 関数やクラスなどの定義の順番は可能な限りトップダウンにする
- 関数Aから関数Bを呼び出す場合、関数Aを前に、関数Bを後ろに定義する

## テスト

- テストコードは`pytest`で書く
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- テストコードは速度と簡潔さを重視する
  - テスト関数を細かく分けず、一連の流れをまとめて1つの関数にする
  - 網羅性のため、必要に応じて`pytest.mark.parametrize`を使用する
- テストコードを書いたら`uv run pytest`でテストを実行する

テストコードの例。

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

## Markdown記述スタイル

- `**`は強調したい箇所のみに使い、箇条書きの見出し用途では使わない
  - NG例: `1. **xx機能**: xxをyyする`
- できるだけmarkdownlintが通るように書く
  - 特に注意するルール:
    - `MD040/fenced-code-language`: Fenced code blocks should have a language specified
- 図はMermaid記法で書く
- 別のMarkdownファイルへのリンクは、基本的に`[プロジェクトルートからのパス](記述個所からの相対パス)`で書く
- lintの実行方法: `uv run pre-commit run --files <file>`
