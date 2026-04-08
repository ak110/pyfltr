# 設定

`pyproject.toml`で設定する。

## 例

```toml
[tool.pyfltr]
preset = "latest"
pylint-args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

## 設定項目

設定項目と既定値は`pyfltr --generate-config`で確認可能。

- preset : プリセット設定(後述)
- {command} : 各コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数
- {command}-fast : `--commands=fast`に含めるか否か(後述)
- jobs : linters/testersの最大並列数(既定値: 4。CLIの`-j`オプションでも指定可能)
- exclude : 除外するファイル名/ディレクトリ名パターン(既定値あり)
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン(既定値は空)

## プリセット設定

`preset`を設定することで、一括して設定を変更できる。
`"latest"` または日付指定 (`"20260330"`, `"20250710"`) が使用可能。

```toml
[tool.pyfltr]
preset = "latest"
```

### preset "20260330" / "latest"

- `pyupgrade = false`
- `autoflake = false`
- `pflake8 = false`
- `isort = false`
- `black = false`
- `ruff-format = true`
- `ruff-check = true`
- `pyright = true`
- `textlint = true`
- `markdownlint = true`

### preset "20250710"

- `pyupgrade = false`
- `autoflake = false`
- `pflake8 = false`
- `isort = false`
- `black = false`
- `ruff-format = true`
- `ruff-check = true`

`preset = "latest"`は予告なく動作を変更する可能性がある。

## ruff-format の 2 段階実行

`ruff-format` は既定で `ruff check --fix --unsafe-fixes` と `ruff format` の 2 ステップを連続実行する。
import ソートや自動修正可能な lint 違反を整形と同時に処理するための挙動。

ステップ 1 の lint 違反 (ruff check の exit 1) は無視され、別途 `ruff-check` コマンドで検出される想定。
設定ミス等による ruff の異常終了 (exit 2 以上) は失敗と判定する。

```toml
[tool.pyfltr]
# ステップ 1 をスキップしたい場合 (既定は true)
ruff-format-by-check = false
# ステップ 1 の引数を差し替えたい場合 (既定は ["check", "--fix", "--unsafe-fixes"])
ruff-format-check-args = ["check", "--fix"]
```

## 並列実行

linters/testersは既定で最大4並列で実行される。
`jobs`で変更可能。

```toml
[tool.pyfltr]
jobs = 8
```

CLIオプション`-j`でも指定でき、`pyproject.toml`より優先される。

実行順は`fast`フラグに基づいて最適化され、`fast = false`のツール（mypy、pylint、pytest等）が先に開始される。

## fastエイリアス

`--commands=fast`で実行されるコマンドは、各コマンドの`{command}-fast`設定で制御される。

```toml
[tool.pyfltr]
# mypyをfastに追加
mypy-fast = true
# pflake8をfastから除外
pflake8-fast = false
```

カスタムコマンドも`fast = true`でfastエイリアスに追加できる（後述）。

## npm系ツール (markdownlint / textlint)

markdownlint-cli2とtextlintはpnpx経由で実行される。
依存パッケージはpnpxの`--package`フラグで`{command}-args`に指定する。

```toml
[tool.pyfltr]
# npxを使う場合
markdownlint-path = "npx"
# グローバルインストール済みのtextlintを直接使う場合
textlint-path = "textlint"
textlint-args = ["--format", "compact"]
```

textlintの既定では`textlint-rule-preset-ja-technical-writing`が含まれる。
追加のプリセットが必要な場合は`textlint-args`をオーバーライドする。

```toml
[tool.pyfltr]
textlint-args = [
    "--package", "textlint",
    "--package", "textlint-rule-preset-ja-technical-writing",
    "--package", "textlint-rule-preset-japanese",
    "textlint", "--format", "compact",
]
```

## カスタムコマンド

`[tool.pyfltr.custom-commands]`で任意のツールを追加できる。

```toml
[tool.pyfltr.custom-commands.bandit]
type = "linter"
path = "bandit"
args = ["-r", "-f", "custom"]
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)'
fast = true
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
- `fast`: `--commands=fast`に含めるか否か（省略時は`false`）

ビルトインコマンド（mypy等）は自動的にエラーパースされる。
カスタムコマンドに対しても`--{name}-args`やenable/disableを使用できる。
