# 設定

`pyproject.toml`で設定する。

## 例

```toml
[tool.pyfltr]
preset = "latest"
pyupgrade-args = ["--py38-plus"]
pylint-args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

## 設定項目

設定項目と既定値は`pyfltr --generate-config`で確認可能。

- preset : プリセット設定(後述)
- {command} : 各コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数
- exclude : 除外するファイル名/ディレクトリ名パターン(既定値あり)
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン(既定値は空)

## プリセット設定

`preset`を設定することで、一括して設定を変更できる。
`"latest"` または日付指定 (`"20250710"`) が使用可能。

```toml
[tool.pyfltr]
preset = "latest"
```

これらのプリセットは、以下の設定を自動的に適用する。

- `pyupgrade = false`
- `autoflake = false`
- `pflake8 = false`
- `isort = false`
- `black = false`
- `ruff-format = true`
- `ruff-check = true`

`preset = "latest"`は予告なく動作を変更する可能性あり。

## カスタムコマンド

`[tool.pyfltr.custom-commands]`で任意のツールを追加できる。

```toml
[tool.pyfltr.custom-commands.bandit]
type = "linter"
path = "bandit"
args = ["-r", "-f", "custom"]
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)'
```

設定項目。

- `type` (必須): `"formatter"` / `"linter"` / `"tester"`
    - formatterは直列実行、linter/testerは並列実行
- `path`: 実行コマンド（省略時はコマンド名）
- `args`: 追加引数（省略時は空）
- `targets`: 対象ファイルパターン（省略時は`"*.py"`）
- `error-pattern`: エラーパース用正規表現（省略可）
    - `file`と`line`と`message`の名前付きグループが必須
    - `col`は任意
    - 指定するとErrorsタブやエラー一覧に表示される

ビルトインコマンド（mypy等）は自動的にエラーパースされる。
カスタムコマンドも`--{name}-args`やenable/disableが使用可能。
