# プロジェクト固有チェックの追加

`[tool.pyfltr.custom-commands]`で任意のツール・独自スクリプトを追加できる。
追加したカスタムコマンドは組み込みのformatter・linter・testerと同じ実行経路で動作する。
pyfltrは並列実行・fixステージ・severity・hints・JSON Lines出力を組み込みツールと同一に扱う。
コーディングエージェント向けの実行基盤へ、プロジェクト固有の検査を統合する場合に利用する。

カスタムコマンドの仕様は[ツール別設定](configuration-tools.md)の「カスタムコマンド」セクションを参照。
導入手順は[はじめに](getting-started.md)を参照。

`error-pattern`は名前付きグループ`file` / `line` / `message`が必須、`col`は任意。
正規表現にこれらが含まれない場合は設定エラーとなる。

## 実運用例（リポジトリ固有スクリプトの統合）

生成ファイルの同期・エンコーディング・リンク切れなどを検査する独自スクリプトを、
組み込みツールと並列に実行するカスタムコマンドとして登録できる。
`type = "linter"`かつ`pass-filenames = false`を指定すると、対象ファイル引数を渡さずにリポジトリ全体を検査する。

```toml
[tool.pyfltr.custom-commands.project-check]
type = "linter"
path = "uv"
args = ["run", "--script", "scripts/check_project.py"]
targets = "*"
pass-filenames = false
```

## Pythonセキュリティ・品質ツール

### bandit（セキュリティチェック）

設定ファイルとして`pyproject.toml`の`[tool.bandit]`または`.bandit`を参照する例。
`config-files`に列挙すると、対象ツール有効化時にいずれもプロジェクトルート直下に存在しない場合に
pyfltrが警告を発行する（ツール自体は実行する）。

```toml
[tool.pyfltr.custom-commands.bandit]
type = "linter"
path = "bandit"
args = ["-r", "-f", "custom"]
targets = "*.py"
error-pattern = '(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)'
config-files = ["pyproject.toml", ".bandit"]
fast = true
```

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
