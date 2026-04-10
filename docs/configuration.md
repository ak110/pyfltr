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

- preset : プリセット設定（後述）
- {command} : 各コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数（lint/fix両モードで常に付与）
- {command}-lint-args : 非fixモード（およびtextlint fix後段のlintチェック) でのみ付与する引数（既定値はtextlintのみ `["--format", "compact"]` を定義）
- {command}-fast : `--commands=fast`に含めるか否か（後述）
- {command}-fix-args : `--fix`時に`{command}-args`の後に追加する引数（既定値はtextlint / markdownlint / ruff-check / eslint / biomeのみ定義）
- prettier-check-args / prettier-write-args : prettierの2段階実行で使う引数（詳細は後述）
- jobs : linters/testersの最大並列数（既定値： 4。CLIの`-j`オプションでも指定可能）
- exclude : 除外するファイル名/ディレクトリ名パターン（既定値あり）
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン（既定値は空）
- respect-gitignore : `.gitignore`に記載されたファイルを除外するか否か（既定: `true`）。gitのルートおよびネストした`.gitignore`、グローバルgitignore、`.git/info/exclude`を全て考慮する。`git`コマンドが必要

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

`ruff-format` は既定で `ruff check --fix --unsafe-fixes` と `ruff format` の2ステップを連続実行する。
importソートや自動修正可能なlint違反を整形と同時に処理するための挙動。

ステップ1のlint違反（ruff checkのexit 1）は無視され、別途 `ruff-check` コマンドで検出される想定。
設定ミス等によるruffの異常終了（exit 2以上）は失敗と判定する。

```toml
[tool.pyfltr]
# ステップ 1 をスキップしたい場合 (既定は true)
ruff-format-by-check = false
# ステップ 1 の引数を差し替えたい場合 (既定は ["check", "--fix", "--unsafe-fixes"])
ruff-format-check-args = ["check", "--fix"]
```

## prettier の 2 段階実行

`prettier`は`--check`（読み取り専用）と`--write`（書き込み）が排他のため、pyfltrは2段階で実行する。

- 通常モード: まず`prettier --check`を実行する
    - `rc == 0` → `succeeded`（整形済み）
    - `rc == 1` → 続けて`prettier --write`を実行し、`rc == 0`なら`formatted`、それ以外は`failed`
    - `rc >= 2` → `failed`（設定ミス等の致命的エラー、`--write`は実行しない）
- `--fix`モード: ステップ1（`--check`）をスキップし、直接`prettier --write`を実行する。ファイル内容ハッシュの変化で`formatted` / `succeeded`を判定する

引数は`prettier-check-args` / `prettier-write-args`で個別に上書きできる（既定はそれぞれ`["--check"]` / `["--write"]`）。共通引数`prettier-args`は両ステップの先頭に付与される。

```toml
[tool.pyfltr]
prettier = true
# キャッシュ等の共通引数
prettier-args = ["--cache"]
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

## npm系ツール (markdownlint / textlint / eslint / prettier / biome)

markdownlint-cli2・textlint・eslint・prettier・biomeは`js-runner`設定で起動方式を切り替える。
既定は`pnpx`で、グローバル/キャッシュから都度取得する従来互換の挙動となる。
プロジェクトの`package.json`で既にこれらのツールをインストール済みの場合は、
`js-runner`を`pnpm` / `npm` / `npx` / `yarn` / `direct`に切り替えるとよい。
これによりCIなどでの再ダウンロードを避けられる。
eslint / prettier / biomeはプラグイン（`typescript-eslint`・`prettier-plugin-svelte`等）を`package.json`で管理するのが一般的。
これらのツールを使うプロジェクトでは`js-runner = "pnpm"`（もしくは`npm` / `yarn` / `direct`）を推奨する。

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
# 共通引数 (lint/fix 両モードで付与)
textlint-args = []
# lint モード専用の引数 (既定で --format compact。builtin パーサが compact 出力を想定している)
textlint-lint-args = ["--format", "compact"]
```

textlintのfix実行 (`textlint --fix`) では `@textlint/fixer-formatter` が使われ、`compact` フォーマッタを解決できない。このため `--format compact` は `textlint-args`（共通）ではなく `textlint-lint-args`（lintモード専用）に分離している。

`pyfltr --fix` 実行時、pyfltrはtextlintを2段階で実行する（fix適用 → lintチェック）ため、
残存違反はcompact形式で正しく取得される。
旧版から `textlint-args = ["--format", "compact", ...]` の設定を引き継いでいる場合でも、
pyfltrはfixステップの起動コマンドから `--format` ペアを自動除去するためクラッシュしない。
新規設定では `textlint-lint-args` に書くことを推奨する。

### textlintのプリセット/ルール指定

textlintで利用するルール/プリセットパッケージは`textlint-packages`に列挙する。既定では`textlint-rule-preset-ja-technical-writing`が含まれる。

```toml
[tool.pyfltr]
textlint-packages = [
    "textlint-rule-preset-ja-technical-writing",
    "textlint-rule-preset-jtf-style",
    "textlint-rule-ja-no-abusage",
]
```

`textlint-packages`は`pnpx` / `npx`モード時に`--package` / `-p`展開される。`pnpm` / `npm` / `yarn` / `direct`モードでは`package.json`側でインストールする前提のため無視される。

### eslint / prettier / biomeの設定

eslint / prettier / biomeはすべて既定で無効。
有効化には`pyproject.toml`で切り替える。
プラグインは`package.json`管理が前提のため、通常は`js-runner = "pnpm"`と併用する。

```toml
[tool.pyfltr]
js-runner = "pnpm"
eslint = true
prettier = true
biome = true
```

既定の引数は以下のとおり。
必要に応じて上書きできる。

- eslint:
    - `eslint-args = ["--format", "json"]`（lint / fix両モードで有効にするため共通argsに配置）
    - `eslint-fix-args = ["--fix"]`
    - 注: ESLint 9系以降で`compact` / `unix` / `tap`等のコアフォーマッタは除去されたため、コア標準の`json`を採用している
    - `eslint-args`を上書きする際は非コアフォーマッタを使わないこと
- prettier:
    - `prettier-check-args = ["--check"]` / `prettier-write-args = ["--write"]`
    - 2段階実行の詳細は「prettierの2段階実行」を参照
- biome:
    - `biome-args = ["check", "--reporter=github"]`（`check`サブコマンドと機械可読出力を共通argsで常時適用）
    - `biome-fix-args = ["--write"]`（safe fixのみ。unsafe fixを使う場合は`["--write", "--unsafe"]`に上書き）
    - 注: `biome-args`の先頭からサブコマンド（`check` / `lint` / `format`）を外すとbiomeがhelp表示で失敗する。必ずサブコマンド名を残すこと

プリセット（`preset = "latest"`）にはeslint / prettier / biomeは含まれない（opt-in）。

## 出力順序

非TUIモード (`--no-ui`、`--ci`、または非対話端末) では、既定で全コマンドの完了後に成功コマンド詳細 → 失敗コマンド詳細 → `summary`の順でまとめて出力する。`pyfltr ... | tail -N`のようにパイプで末尾だけ切り出してもsummaryと失敗情報が末尾に残るため、Claude Codeなど末尾だけを読み取るツールでも実行結果を把握できる。

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

- `type`（必須）: `"formatter"` / `"linter"` / `"tester"`
    - formatterは直列実行、linter/testerは並列実行
- `path`: 実行コマンド（省略時はコマンド名）
- `args`: 追加引数（省略時は空）
- `targets`: 対象ファイルパターン（省略時は`"*.py"`）
- `error-pattern`: エラーパース用正規表現（省略可）
    - `file`と`line`と`message`の名前付きグループが必須
    - `col`は任意
    - 指定するとErrorsタブやエラー一覧に表示される
- `fast`: `--commands=fast`に含めるか否か（省略時は`false`）
- `fix-args`: `pyfltr --fix`時に`args`の後ろへ追加する引数（省略時はfixモード対象外）

ビルトインコマンド（mypy等）は自動的にエラーパースされる。
カスタムコマンドに対しても`--{name}-args`やenable/disableを使用できる。

### カスタムコマンドでの fix モード対応

autofix機能を持つツールをカスタムコマンドとして登録する場合は、`fix-args`を定義しておくと`pyfltr --fix`の対象に含まれる。

```toml
[tool.pyfltr.custom-commands.my-linter]
type = "linter"
path = "my-linter"
args = ["--check"]
fix-args = ["--fix"]
```

fixモードでは`args`の後に`fix-args`が追加され、`my-linter --check --fix <files>`として実行される。
