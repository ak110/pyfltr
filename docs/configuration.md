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
- {command}-fix-args : `--fix`時に`{command}-args`の後に追加する引数(既定値は textlint / markdownlint / ruff-check のみ定義)
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

markdownlint-cli2とtextlintは`js-runner`設定で起動方式を切り替える。既定は`pnpx`で、グローバル/キャッシュから都度取得する従来互換の挙動となる。プロジェクトの`package.json`で既にtextlint / markdownlint-cli2をインストールしている場合は、`js-runner`を`pnpm` / `npm` / `npx` / `yarn` / `direct`のいずれかに切り替えるとCIなどでの再ダウンロードを避けられる。

```toml
[tool.pyfltr]
# プロジェクトの node_modules を使う (pnpm exec 経由)
js-runner = "pnpm"
```

| `js-runner` | 挙動 |
| --- | --- |
| `pnpx` | `pnpx`経由で起動する (既定)。`textlint-packages`は`--package`で展開される |
| `pnpm` | `pnpm exec`経由で起動する。パッケージは`package.json`側で管理する |
| `npm` | `npm exec --no --`経由で起動する |
| `npx` | `npx --no-install`経由で起動する。`textlint-packages`は`-p`で展開される |
| `yarn` | `yarn run`経由で起動する |
| `direct` | `node_modules/.bin/`配下の実行ファイルを直接起動する |

`{command}-path`を明示的に設定した場合はその値が優先され、自動解決は無効化される。グローバルインストール済みのtextlintを直接使いたい場合などに利用する。

```toml
[tool.pyfltr]
textlint-path = "textlint"
textlint-args = ["--format", "compact"]
```

### textlintのプリセット/ルール指定

textlintで利用するルール/プリセットパッケージは`textlint-packages`に列挙する。既定では`textlint-rule-preset-ja-technical-writing`が含まれる。

```toml
[tool.pyfltr]
textlint-packages = [
    "textlint-rule-preset-ja-technical-writing",
    "textlint-rule-preset-japanese",
    "textlint-rule-ja-no-abusage",
]
```

`textlint-packages`は`pnpx` / `npx`モード時に`--package` / `-p`展開される。`pnpm` / `npm` / `yarn` / `direct`モードでは`package.json`側でインストールする前提のため無視される。

## 出力順序

非TUIモード (`--no-ui`、`--ci`、または非対話端末) では、既定で全コマンドの完了後に`summary` → 成功コマンド詳細 → 失敗コマンド詳細の順でまとめて出力する。`pyfltr ... | tail -N`のようにパイプで末尾だけ切り出しても、エラー情報が末尾に残る設計となっている。

従来の「各コマンドの完了時に即座に詳細ログを出す」挙動を使いたい場合は`--stream`を指定する。

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
- `fix-args`: `pyfltr --fix`時に`args`の後ろへ追加する引数（省略時は fix モード対象外）

ビルトインコマンド（mypy等）は自動的にエラーパースされる。
カスタムコマンドに対しても`--{name}-args`やenable/disableを使用できる。

### カスタムコマンドでの fix モード対応

autofix 機能を持つツールをカスタムコマンドとして登録する場合は、`fix-args`を定義しておくと`pyfltr --fix`の対象に含まれる。

```toml
[tool.pyfltr.custom-commands.my-linter]
type = "linter"
path = "my-linter"
args = ["--check"]
fix-args = ["--fix"]
```

fix モードでは`args`の後に`fix-args`が追加され、`my-linter --check --fix <files>`として実行される。
