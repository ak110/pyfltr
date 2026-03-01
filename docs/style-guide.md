# スタイルガイド

## 全般

- ソースコード上での定義順に注意し、安易に末尾に追加してはならない
  - 言語機能の許す限りにおいて、定義順は以下の指針に従う:
    - 一般的なもの、使用頻度の高いものを上に、特殊なもの、使用頻度の低いものを下に配置する
      - 例: 公開API（外部から参照されるもの）＞主要フロー（読み始め地点）＞補助（private helper）＞低レベル（I/Oや薄いラッパ）
    - 可能な限りトップダウンにする
      つまり関数Aから関数Bを呼び出す場合、関数Aを前に、関数Bを後ろに定義する
      (呼び出す側が上、呼び出される側が下)
    - 以上を満たす範囲内で、可能であれば関連するものは近くにまとめる
- テストコードは速度と簡潔さを重視
  - sleepなどflakyな処理は避ける（使用時は0.1秒単位、関数全体で1秒以内を目標とする）
  - テストデータは可能な限り共通化する
  - モック・モンキーパッチは可能な限り避けて実コードを通す
- コメントやログは日本語で書く
- コメントは以下の内容に限る
  - 「なぜそうするか（設計上の制約、トレードオフ、バグ回避理由）」
  - 「何をしているか（数行以上のコードの塊に対して短い要約として）」

## Python実装

- importについて
  - 可能な限り`import xxx`形式で書く (`from xxx import yyy`ではなく)
  - 可能な限りトップレベルでimportする (循環参照や初期化順による問題を避ける場合に限りブロック内も可)
- タイプヒントは可能な限り書く
  - `typing.List`ではなく`list`を使用する。`dict`やその他も同様。
  - `typing.Optional`ではなく`| None`を使用する。
- docstringは基本的には概要のみ書く
- ログは`logging`を使う
- 日付関連の処理は`datetime`を使う
- ファイル関連の処理は`pathlib`を使う (`open`関数や`os`モジュールは使わない)
- テーブルデータの処理には`polars`を使う (`pandas`は使わない)
- パッケージ管理には`uv`を使う
- .venvの更新には`make update`を使う
- コードを書いた後は必ず`make test`する。コードフォーマット、mypy、pytestなどがまとめて実行される
- 新しいファイルを作成する場合は近い階層の代表的なファイルを確認し、可能な限りスタイルを揃える
- インターフェースの都合上未使用の引数がある場合は、関数先頭で`del xxx # noqa`のように書く(lint対策)
- `typing.Literal`の分岐は`typing.assert_never`で網羅性を担保（`else: typing.assert_never(x)`）
- 単なる長い名前の別名でしかないローカル変数は作らない。例えば `x = cls.foo` と書いて `x` を使うより `cls.foo` を直接使う。
- Lintエラーの対策は、可能な限り`assert`や`del`などの通常の構文を使用する
  - Linter側のバグなどでどうしても回避が難しい、あるいは必要以上に複雑になってしまう場合に限り`# type: ignore[xxx]`などを使用する
    (ただし、`mypy`、`pyright`, `pylint`などが重複して検出しちゃうケースも多く、そうなると無視コメントが入り乱れるためあくまでも最終手段)

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
