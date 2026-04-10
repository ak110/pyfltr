# 使い方

## サブコマンド

pyfltrはサブコマンドで動作モードを指定する。

```shell
pyfltr <subcommand> [files and/or directories ...]
```

対象を指定しなかった場合は、カレントディレクトリを指定した場合と同じ扱いとなる。

指定したファイルやディレクトリの配下のうち、各コマンドのtargetsパターンに一致するファイルのみ処理される。

- Python系ツール: `*.py`
- markdownlint / textlint: `*.md`
- pytest: `*_test.py`

### ci（既定）

```shell
pyfltr ci [files and/or directories ...]
pyfltr [files and/or directories ...]  # ciは省略可能
```

全チェック実行。CI環境やコミット前の検証に適する。

終了コード:

- 0: Formattersによるファイル変更が無く、かつLinters/Testersでのエラーも無い場合
- 1: 上記以外の場合

### run

```shell
pyfltr run [files and/or directories ...]
```

全チェック実行。Formattersによるファイル変更があってもLinters/Testersでのエラー無しなら終了コードは0になる。ローカルでの全チェック実行に適する。

### fast

```shell
pyfltr fast [files and/or directories ...]
```

mypy / pylint / pytestなど重いコマンドを除外した軽量チェック。Formattersによるファイル変更があっても終了コードは0になる。pre-commitフックなど速度を優先する場面に適する。

既定で含まれるコマンド: `pyupgrade` `autoflake` `isort` `black` `ruff-format` `ruff-check` `pflake8` `ty` `markdownlint` `textlint`

含まれるコマンドは各コマンドの`{command}-fast`設定で制御できる（[設定](configuration.md)を参照）。

### fix

```shell
pyfltr fix [files and/or directories ...]
```

修正モード。linterの中でもautofix機能を持つtextlint / markdownlint / ruff-checkなどに、内部で `--fix` 相当の引数を追加して実行する。手動実行専用。

fixモードの対象は次の和集合となる。

- 有効化されたformatter全て（通常実行そのものがファイルを修正する）
- 有効化されており、かつ `{command}-fix-args` が定義されたlinter（ビルトインではtextlint / markdownlint / ruff-check / eslint / biomeが既定で対応）

fixモードの挙動。

- 全対象コマンドを順次実行する（同一ファイルへの書き込み競合を避けるため並列化しない）
- `--shuffle` は無効化される
- 対象が0件の場合はエラー終了する
- `--commands` と併用可能。併用時は展開後の結果に対して上記フィルタを適用する
- linterのfix実行後、ファイルmtimeの変化があれば `formatted`、変化が無ければ `succeeded`、終了コードが0以外なら `failed` となる
    - 特にruff-checkは未修正の違反が残ると終了コード1を返すため、`failed` 扱いとなる。通常モードの `ruff-check` で残存違反を別途確認すること

カスタムコマンドでも `pyproject.toml` の `[tool.pyfltr.custom-commands.<name>]` に `fix-args = [...]` を定義すればfixモードの対象にできる。

## 特定のツールのみ実行

```shell
pyfltr ci \
  --commands=pyupgrade,autoflake,isort,black,ruff-format,\
ruff-check,pflake8,mypy,pylint,pyright,ty,markdownlint,textlint,pytest \
  [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。全サブコマンドで使用可能。

以下のエイリアスも使用可能。(例: `--commands=format`)

- `format`: `pyupgrade` `autoflake` `isort` `black` `ruff-format`
- `lint`: `ruff-check` `pflake8` `mypy` `pylint` `pyright` `ty` `markdownlint` `textlint`
- `test`: `pytest`
- `fast`: per-commandの`{cmd}-fast`フラグがtrueのコマンド

※ `pyproject.toml`の`[tool.pyfltr]`で無効になっているコマンドは無視される。

## UI

ターミナル上で実行すると、TextualベースのTUIが自動的に有効になる。

- Summaryタブ: 各コマンドのステータス・エラー数・経過時間をリアルタイム表示
- Errorsタブ: エラー発生時のみ出現し、全コマンドのエラー箇所を`ファイル:行番号`形式で一覧表示
- 各コマンドタブ: コマンドの出力をリアルタイム表示

Errorsタブのエラー一覧は`ファイル:行番号: [コマンド名] メッセージ`形式で、
VSCodeのターミナルからクリックして該当箇所にジャンプできる。

- `--no-ui`: UIを無効化し、出力を直接ターミナルに表示（エラー一覧の後にサマリーを表示）
- `--ci`: CI環境向け（`--no-shuffle --no-ui` 相当）
- `-j N` / `--jobs N`: linters/testersの最大並列数を指定（既定: 4、`pyproject.toml`でも設定可能）

その他のオプションは `pyfltr --help` を参照。
