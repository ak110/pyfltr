# 設定項目

`pyproject.toml`で設定する。

## 例

```toml
[tool.pyfltr]
preset = "latest"
pylint-args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

## 設定項目一覧

設定項目と既定値は`pyfltr generate-config`で確認可能。

- preset : プリセット設定（後述）
- python : Python系ツールの一括有効/無効（後述）
- {command} : 各コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数（lint/fix両モードで常に付与）
- {command}-lint-args : 非fixモード（およびtextlint fix後段のlintチェック) でのみ付与する引数（既定値はtextlintのみ `["--format", "compact"]` を定義）
- {command}-fast : `fast`サブコマンドに含めるか否か（後述）
- {command}-fix-args : `fix`サブコマンド時に`{command}-args`の後に追加する引数（既定値はtextlint / markdownlint / ruff-check / eslint / biomeのみ定義）
- {command}-targets : 対象ファイルパターンの完全上書き（[ツール別設定](configuration-tools.md)を参照）
- {command}-extend-targets : 対象ファイルパターンへの追加（[ツール別設定](configuration-tools.md)を参照）
- {command}-pass-filenames : ファイル引数をコマンドに渡すか否か（既定: `true`。[ツール別設定](configuration-tools.md)を参照）
- {command}-version : bin-runner対応ツールのバージョン指定（既定: `"latest"`。[ツール別設定](configuration-tools.md)を参照）
- prettier-check-args / prettier-write-args : prettierの2段階実行で使う引数（[ツール別設定](configuration-tools.md)を参照）
- shfmt-check-args / shfmt-write-args : shfmtの2段階実行で使う引数（[ツール別設定](configuration-tools.md)を参照）
- pylint-pydantic : pylint実行時に`--load-plugins=pylint_pydantic`を自動追加するか（既定: `true`、後述）
- mypy-unused-awaitable : mypy実行時に`--enable-error-code=unused-awaitable`を自動追加するか（既定: `true`、後述）
- jobs : linters/testersの最大並列数（既定値： 4。CLIの`-j`オプションでも指定可能）
- exclude : 除外するファイル名/ディレクトリ名パターン（既定値あり）
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン（既定値は空）
- respect-gitignore : `.gitignore`に記載されたファイルを除外するか否か（既定: `true`）。gitのルートおよびネストした`.gitignore`、グローバルgitignore、`.git/info/exclude`を全て考慮する。`git`コマンドが必要
- pre-commit-auto-skip : `.pre-commit-config.yaml`からpyfltr関連hookを自動検出してSKIP環境変数に追加するか（既定: `true`）
- pre-commit-skip : SKIP環境変数に渡すhook IDの手動指定リスト（`pre-commit-auto-skip`と併用可能）

## プリセット設定

`preset`を設定することで、一括して設定を変更できる。
`"latest"` または日付指定 (`"20260413"`, `"20260411"`, `"20260330"`, `"20250710"`) が使用可能。

```toml
[tool.pyfltr]
preset = "latest"
```

`preset = "latest"`は予告なく動作を変更する可能性がある。

### preset "20260413" / "latest"

- preset "20260411"に加えて以下の設定が行われる
- `pre-commit = true`

### preset "20260411"

- preset "20260330"に加えて以下の設定が行われる
- `actionlint = true`
- `typos = true`
- `uv-sort = true`

### preset "20260330"

- preset "20250710"に加えて以下の設定が行われる
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

## Python系ツールの一括無効化

`python = false`を設定すると、Python系のツールを一括で無効化する。JS/TS専用プロジェクトで設定を簡潔にする場合に使う。

対象ツール:

- pyupgrade / autoflake / isort / black / ruff-format / ruff-check
- pflake8 / mypy / pylint / pyright / ty / pytest / uv-sort

```toml
[tool.pyfltr]
python = false
js-runner = "pnpm"
eslint = true
prettier = true
```

以下のツールは`python`設定の影響を受けない。

- npm系ツール: markdownlint / textlint / eslint / prettier / biome / oxlint / tsc / vitest
- bin-runner対応ツール: ec / shellcheck / shfmt / typos / actionlint

適用優先度は `preset < python < 個別設定`。`python = false`でも`mypy = true`のように個別に有効化できる。

## 自動オプション

各ツールの望ましいオプションを自動的にコマンドラインに追加する。`{command}-args`とは独立して動作する。

| 設定 | 既定 | 自動追加される引数 |
| --- | --- | --- |
| `pylint-pydantic` | `true` | `--load-plugins=pylint_pydantic` |
| `mypy-unused-awaitable` | `true` | `--enable-error-code=unused-awaitable` |

自動引数は`{command}-args`やCLI引数と重複しないよう排除される。不要な場合は`false`に設定する。

```toml
[tool.pyfltr]
pylint-pydantic = false
mypy-unused-awaitable = false
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

`fast`サブコマンドで実行されるコマンドは、各コマンドの`{command}-fast`設定で制御される。

```toml
[tool.pyfltr]
# mypyをfastに追加
mypy-fast = true
# pflake8をfastから除外
pflake8-fast = false
```

カスタムコマンドも`fast = true`でfastエイリアスに追加できる。

## 出力順序

非TUIモード (`--no-ui`、`--ci`、または非対話端末) では、既定で全コマンドの完了後に成功コマンド詳細 → 失敗コマンド詳細 → `summary`の順でまとめて出力する。`pyfltr ... | tail -N`のようにパイプで末尾だけ切り出してもsummaryと失敗情報が末尾に残るため、Claude Codeなど末尾だけを読み取るツールでも実行結果を把握できる。

従来の「各コマンドの完了時に即座に詳細ログを出す」挙動を使いたい場合は`--stream`を指定する。

---

個別のツール設定（2段階実行、ファイルパターン、bin-runner、npm系ツール、カスタムコマンド等）は[ツール別設定](configuration-tools.md)を参照。
