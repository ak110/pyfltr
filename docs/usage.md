# 使い方

## 通常

```shell
pyfltr [files and/or directories ...]
```

対象を指定しなければカレントディレクトリを指定したのと同じ扱い。

指定したファイルやディレクトリの配下のうち、各コマンドのtargetsパターンに一致するファイルのみ処理される。

- Python系ツール: `*.py`
- markdownlint / textlint: `*.md`
- pytest: `*_test.py`

終了コード。

- 0: Formattersによるファイル変更無し、かつLinters/Testersでのエラー無し
- 1: 上記以外

`--exit-zero-even-if-formatted`を指定すると、Formattersによるファイル変更があっても
Linters/Testersでのエラー無しなら終了コードは0になる。

## 特定のツールのみ実行

```shell
pyfltr \
  --commands=pyupgrade,autoflake,isort,black,ruff-format,\
ruff-check,pflake8,mypy,pylint,pyright,markdownlint,textlint,pytest \
  [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。

以下のエイリアスも使用可能。(例: `--commands=fast`)

- `format`: `pyupgrade` `autoflake` `isort` `black` `ruff-format`
- `lint`: `ruff-check` `pflake8` `mypy` `pylint` `pyright` `markdownlint` `textlint`
- `test`: `pytest`
- `fast`: per-commandの`{cmd}-fast`フラグがtrueのコマンド（デフォルト: `pyupgrade` `autoflake` `isort` `black` `ruff-format` `ruff-check` `pflake8` `markdownlint` `textlint`）

※ `pyproject.toml`の`[tool.pyfltr]`で無効になっているコマンドは無視される。

## UI

ターミナル上で実行すると、Textual ベースの TUI が自動的に有効になる。

- Summaryタブ: 各コマンドのステータス・エラー数・経過時間をリアルタイム表示
- Errorsタブ: エラー発生時のみ出現し、全コマンドのエラー箇所を`ファイル:行番号`形式で一覧表示
- 各コマンドタブ: コマンドの出力をリアルタイム表示

Errorsタブのエラー一覧は`ファイル:行番号: [コマンド名] メッセージ`形式で、
VSCodeのターミナルからクリックして該当箇所にジャンプできる。

- `--no-ui`: UIを無効化し、出力を直接ターミナルに表示（エラー一覧はサマリー後に表示）
- `--ci`: CI環境向け (`--no-shuffle --no-ui` 相当)

その他のオプションは `pyfltr --help` を参照。
