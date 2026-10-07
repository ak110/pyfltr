# プロジェクト固有チェックの追加

`[tool.pyfltr.custom-commands]`で任意のツール・独自スクリプトを追加できる。
追加したカスタムコマンドは組み込みのformatter・linter・testerと同じ実行の流れで動作する。
pyfltrは並列実行・fixステージ・severity・hints・JSON Lines出力を組み込みツールと同一に扱う。
コーディングエージェント向けの実行基盤へ、プロジェクト固有のチェックを統合する場合に利用する。

導入手順は[はじめに](getting-started.md)を参照。

`error-pattern`は名前付きグループ`file` / `line` / `message`が必須、`col`は任意。
正規表現にこれらが含まれない場合は設定エラーとなる。

## カスタムコマンドの仕様

`[tool.pyfltr.custom-commands]`で任意のツールを追加できる。

```toml
[tool.pyfltr.custom-commands.mytool]
type = "linter"
path = "mytool"
args = ["-r", "-f", "custom"]
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)'
config-files = [".mytoolrc", "pyproject.toml"]
fast = true
```

設定項目。

- `type`（必須）: `"formatter"` / `"linter"` / `"tester"`
    - formatterは直列実行、linter/testerは並列実行
- `path`: 実行コマンド（省略時はコマンド名）
- `args`: 追加引数（省略時は空）
- `extend-args`: `args`の末尾へ追加する引数（省略時は空）。
  `args`既定値を保ったまま要素を足したい場合に使う
- `targets`: 対象ファイルパターン（省略時は`"*.py"`）
- `error-pattern`: エラーパース用正規表現（省略可）
    - `file`と`line`と`message`の名前付きグループが必須
    - `col`は任意
    - 指定するとErrorsタブやエラー一覧に表示される
- `fast`: `fast`サブコマンドに含めるか否か（省略時は`false`）
- `fix-args`: fix段で`args`の後ろへ追加する引数（省略時はfix段の対象外）
- `pass-filenames`: ファイル引数をコマンドに渡すか否か（省略時は`true`）。
  プロジェクト全体を一括チェックするツールでは`false`に設定する
- `config-files`: このコマンドの設定ファイル候補のリスト（省略時は空）。globパターン可。
  有効化時にどれもプロジェクトルート直下に見つからないとpyfltrが警告を発行する（ツール自体は実行する）。
  pre-commit・prekなどの「設定ファイル無しでは機能しないツール」の設定不備を可視化する用途。
  判定はモノレポでも起点cwd直下のみを対象とし、サブプロジェクトのディレクトリでは行わない
- `severity`: `"error"`（既定）または`"warning"`。
  詳細は[severityによる失敗の警告化](configuration.md#severity)を参照
- `hints`: LLM向けの追加文言（文字列の配列、省略時は空）。
  詳細は[hintsによるLLM向け補足](configuration.md#hints)を参照

ビルトインコマンド（mypy等）は自動的にエラーパースされる。
カスタムコマンドに対しても`--{name}-args`やenable/disableを使用できる。

`pyfltr run` / `pyfltr ci` のように`--commands`を省略したサブコマンドでは、
登録済みの有効なカスタムコマンドもビルトインと同様にデフォルトの実行対象に含まれる。
特定のツールだけを動かしたい場合は`--commands=svelte-check`のように明示指定する。

### カスタムコマンドでの fix モード対応

autofix機能を持つツールをカスタムコマンドとして登録する場合は、`fix-args`を定義しておくと`run`/`fast`のfix段に含まれる。

```toml
[tool.pyfltr.custom-commands.my-linter]
type = "linter"
path = "my-linter"
args = ["--check"]
fix-args = ["--fix"]
```

fixモードでは`args`の後に`fix-args`が追加され、`my-linter --check --fix <files>`として実行される。

## 実運用例（リポジトリ固有スクリプトの統合）

生成ファイルの同期・エンコーディング・リンク切れなどをチェックする独自スクリプトを、
組み込みツールと並列に実行するカスタムコマンドとして登録できる。
`type = "linter"`かつ`pass-filenames = false`を指定すると、対象ファイル引数を渡さずにリポジトリ全体をチェックする。

```toml
[tool.pyfltr.custom-commands.project-check]
type = "linter"
path = "uv"
args = ["run", "--script", "scripts/check_project.py"]
targets = "*"
pass-filenames = false
```

## Pythonセキュリティ・品質ツール

`bandit`は組み込みツールとして利用する。[banditの設定](configuration-tools.md#bandit)を参照。

### deptry（未使用・不足依存の検出）

```toml
[tool.pyfltr.custom-commands.deptry]
type = "linter"
path = "deptry"
args = ["."]
targets = "*.py"
pass-filenames = false
```

### vulture（未使用コードの検出）

```toml
[tool.pyfltr.custom-commands.vulture]
type = "linter"
path = "vulture"
args = []
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\d+):\s*(?P<message>.+)'
fast = true
```

### detect-secrets（シークレット検出）

```toml
[tool.pyfltr.custom-commands.detect-secrets]
type = "linter"
path = "detect-secrets"
args = ["scan", "--list-all-plugins"]
targets = "*.py"
pass-filenames = false
```

## 汎用ツール

### codespell（スペルチェック）

`fix-args`を定義するとfix段で`args`の後ろに追加されて`--write-changes`付きで実行される。

```toml
[tool.pyfltr.custom-commands.codespell]
type = "linter"
path = "codespell"
args = []
fix-args = ["--write-changes"]
targets = ["*.py", "*.md", "*.rst", "*.txt"]
fast = true
```

### cspell（スペルチェック、npm系）

js-runner対応のスペルチェッカー。`package.json`でインストールする前提で、`js-runner = "pnpm"`と併用する。

```toml
[tool.pyfltr]
js-runner = "pnpm"

[tool.pyfltr.custom-commands.cspell]
type = "linter"
path = "cspell"
args = ["lint", "--no-progress", "--no-summary"]
targets = ["*.py", "*.md", "*.ts", "*.js"]
error-pattern = '(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)\s*-\s*(?P<message>.+)'
fast = true
```

## JS/TSプロジェクト向け

### svelte-check（Svelteの型チェック）

```toml
[tool.pyfltr.custom-commands.svelte-check]
type = "linter"
path = "svelte-check"
args = ["--tsconfig", "./tsconfig.json"]
targets = "*.svelte"
pass-filenames = false
```

### commitlint（コミットメッセージチェック）

```toml
[tool.pyfltr.custom-commands.commitlint]
type = "linter"
path = "commitlint"
args = ["--from=HEAD~1"]
pass-filenames = false
```
