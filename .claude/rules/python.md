---
paths:
  - "**/*.py"
---

# Python記述スタイル

- importについて
  - 可能な限り`import xxx`形式で書く (`from xxx import yyy`ではなく)
  - `import xxx as yyy` の別名は`np`などの一般的なものを除き極力使用しない
  - 可能な限りトップレベルでimportする (循環参照や初期化順による問題を避ける場合に限りブロック内も可)
- タイプヒントは可能な限り書く
  - `typing.List`ではなく`list`を使用する。`dict`やその他も同様。
  - `typing.Optional`ではなく`| None`を使用する
  - 関数をオーバーライドする場合は`typing.override`デコレーターを必ず使用する
- docstringはGoogle Style
  - 自明なArgs, Returns, Raisesは省略する
- ログは`logging`を使う
  - `logger = logging.getLogger(__name__)`でモジュールごとに取得
  - `exc_info=True`指定時は例外をメッセージへ含めず簡潔に（例: `logger.error("〇〇処理エラー", exc_info=True)`）
    - 頻出する例外に限り `logger.warning(f"〇〇失敗: {e}")` のように文字列化して出力する
  - 一度のエラーで複数回ログが出力されたり、逆に一度もログが出なかったりすることが無いよう注意する
- 日付関連の処理は`datetime`を使う
- ファイル関連の処理は`pathlib`を使う (`open`関数や`os`モジュールは使わない)
- テーブルデータの処理には`polars`を使う (`pandas`は使わない)
- パッケージ管理には`uv`を使う
- インターフェースの都合上未使用の引数がある場合は、関数先頭で`del xxx # noqa`のように書く(lint対策)
- `typing.Literal`の分岐は`typing.assert_never`で網羅性を担保（`else: typing.assert_never(x)`）
- 単なる長い名前の別名でしかないローカル変数は作らない。例えば `x = cls.foo` と書いて `x` を使うより `cls.foo` を直接使う。
- SQLAlchemyのNULLチェックは`.is_(None)`を使用
- Lintエラーの対策は、可能な限り`assert`や`del`などの通常の構文を使用する
  - Linter側のバグなどで回避が難しい、あるいは必要以上の複雑さを招く場合のみ`# type: ignore[xxx]`などを使用する。
    `mypy`・`pyright`・`pylint`などが重複検出するケースも多く、無視コメントが入り乱れるためあくまで最終手段とする
- Python 3.14以降: PEP 758により`except ValueError, TypeError:`のようにかっこなしで複数例外を書ける（フォーマッターが自動整形する場合あり）
