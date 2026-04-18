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

設定項目と既定値は`pyfltr generate-config`で確認可能。`{command}`系の項目およびツール固有の項目（`prettier-check-args`など）の詳細はツール別設定ページを参照。

- preset : プリセット設定（後述）
- python : Python系ツールのゲート開閉（後述）
- javascript : JavaScript / TypeScript系ツールのゲート開閉（後述）
- rust : Rust系ツールのゲート開閉（後述）
- dotnet : .NET系ツールのゲート開閉（後述）
- {command} : 各コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数（lint/fix両モードで常に付与）
- {command}-lint-args : 非fixモードで付与する引数（既定はtextlintのみ`["--format", "compact"]`）
- {command}-fast : `fast`サブコマンドに含めるか否か（後述）
- {command}-fix-args : fix段で`{command}-args`の後に追加する引数（既定値はtextlint / markdownlint / ruff-check / eslint / biomeのみ定義）
- {command}-targets : 対象ファイルパターンの完全上書き
- {command}-extend-targets : 対象ファイルパターンへの追加
- {command}-exclude : ツール別の追加除外パターン（後述）
- {command}-pass-filenames : ファイル引数をコマンドに渡すか否か（既定: `true`）
- {command}-version : bin-runner対応ツールのバージョン指定（既定: `"latest"`）
- pylint-pydantic : pylint実行時に`--load-plugins=pylint_pydantic`を自動追加するか（既定: `true`、後述）
- mypy-unused-awaitable : mypy実行時に`--enable-error-code=unused-awaitable`を自動追加するか（既定: `true`、後述）
- jobs : linters/testersの最大並列数（既定: 4。CLIの`-j`オプションでも指定可能）
- exclude : 除外するファイル名/ディレクトリ名パターン（既定値あり）
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン（既定は空）
- respect-gitignore : `.gitignore`に記載されたファイルを除外するか否か（既定: `true`）。gitのルートおよびネストした`.gitignore`、グローバルgitignore、`.git/info/exclude`を全て考慮する。`git`コマンドが必要
- pre-commit-auto-skip : `.pre-commit-config.yaml`からpyfltr関連hookを自動検出してSKIP環境変数に追加するか（既定: `true`）
- pre-commit-skip : SKIP環境変数に渡すhook IDの手動指定リスト（`pre-commit-auto-skip`と併用可能）
- archive : 実行アーカイブの有効/無効（既定: `true`。`--no-archive`で実行単位に無効化）
- archive-max-runs : 保存する最大世代数（既定: 100。0以下で世代軸の自動削除を無効化）
- archive-max-size-mb : アーカイブ全体の合計サイズ上限（既定: 1024 MB。0以下でサイズ軸の自動削除を無効化）
- archive-max-age-days : 保存期間の上限（日数。既定: 30。0以下で期間軸の自動削除を無効化）
- cache : ファイルhashキャッシュの有効/無効（既定: `true`。`--no-cache`で実行単位に無効化）
- cache-max-age-hours : キャッシュエントリの保存期間（時間。既定: 12。0以下で期間軸の自動削除を無効化）
- jsonl-diagnostic-limit : 1ツールあたりのdiagnostic出力件数上限（既定: 0 = 無制限）
- jsonl-message-max-lines : `tool.message`の行数上限（既定: 30）
- jsonl-message-max-chars : `tool.message`の文字数上限（既定: 2000）

`prettier-check-args` / `prettier-write-args` / `shfmt-check-args` / `shfmt-write-args`などの2段階実行向け引数はツール別設定ページで詳しく扱う。

## プリセット設定

プリセットは各時点での推奨ツール構成をバージョン付きで示すスナップショット。
`"latest"`または日付指定（`"20260330"` / `"20260411"` / `"20260413"`）を指定する。

```toml
[tool.pyfltr]
preset = "latest"
```

`preset = "latest"`はpyfltrの更新に伴って対象ツールの追加や既定値の変更が予告なく入ることがある。
破壊的変更を避けたい場合は日付指定プリセットで固定すると、当該日時点の構成をそのまま維持できる。

プリセットには言語別ツールも含まれるが、ユーザー側で明示しない限り該当ツールは実行されない。
次節の言語カテゴリキーで「どの言語のツールを通過させるか」を明示する。

### preset "20260330"

以下の設定が行われる。

- `ruff-format = true`
- `ruff-check = true`
- `pyright = true`
- `textlint = true`
- `markdownlint = true`

### preset "20260411"

`"20260330"`に加えて以下が有効化される。

- `actionlint = true`
- `typos = true`
- `uv-sort = true`

### preset "20260413" / "latest"

`"20260411"`に加えて以下が有効化される。

- `pre-commit = true`

## 言語カテゴリによるゲート制御

各言語カテゴリに属するツールは既定で無効（opt-in）。
プロジェクトで利用する言語カテゴリキーを`true`にすると、プリセットで推奨された当該言語ツールが有効化される。
カテゴリキーを`false`（既定）にすると、プリセットで推奨された当該言語ツールもゲートで`false`へ押し戻される。

個別のツール単位では`{command} = true`での有効化・`{command} = false`での無効化も可能で、適用優先度は`preset < 言語カテゴリゲート < 個別設定`。

```toml
[tool.pyfltr]
preset = "latest"
python = true
```

各言語カテゴリキーとゲート対象ツールは次の通り。

- `python`: ruff-format・ruff-check・mypy・pylint・pyright・ty・pytest・uv-sort
- `javascript`: eslint・biome・oxlint・prettier・tsc・vitest（TypeScriptも同一カテゴリ）
- `rust`: cargo-fmt・cargo-clippy・cargo-check・cargo-test・cargo-deny
- `dotnet`: dotnet-format・dotnet-build・dotnet-test

カテゴリ同士は独立して作用する。
たとえば`python = true`を指定してもJavaScript系やRust系のツールは有効化されない。
Pythonプロジェクトで利用する場合は別途`pip install pyfltr[python]`で依存を導入する必要がある。
JavaScript系・Rust系・.NET系は各言語のツールチェイン（Node.js・cargo・dotnet CLI）が前提となる。

プリセットに含まれない言語別ツール（例: `mypy` / `pytest` は現行プリセットに含まれない）を使いたい場合は個別に`{command} = true`を指定する。
個別指定はゲートを越えて最優先される。

```toml
[tool.pyfltr]
preset = "latest"
python = true
mypy = true      # preset に無いため個別指定で追加
pytest = true
```

## ツール別除外設定

`{command}-exclude`を設定すると、特定ツールにのみ適用する追加の除外パターンを指定できる。
全体の`exclude`/`extend-exclude`による除外はこれとは独立して事前に適用される。

```toml
[tool.pyfltr]
# mypy だけ vendor/ と gen_*.py を除外する
mypy-exclude = ["vendor", "gen_*.py"]
```

パターンの書式は`extend-exclude`と同じflake8風のglobパターンで、ディレクトリ指定はその配下も除外される。
`--no-exclude`を指定した場合、全体の`exclude`/`extend-exclude`と合わせてツール別除外も無効化される。

`pass-filenames = false`のツール（pre-commit・tsc・cargo-\*・dotnet-\*など）はファイル名をコマンドに渡さないため、`{command}-exclude`を設定しても効果がない。

## 自動オプション

各ツールの望ましいオプションを自動的にコマンドラインに追加する。
`{command}-args`とは独立して動作する。

| 設定 | 既定 | 自動追加される引数 |
| --- | --- | --- |
| `pylint-pydantic` | `true` | `--load-plugins=pylint_pydantic` |
| `mypy-unused-awaitable` | `true` | `--enable-error-code=unused-awaitable` |

自動引数は`{command}-args`やCLI引数と重複しないよう排除される。
不要な場合は`false`に設定する。

```toml
[tool.pyfltr]
pylint-pydantic = false
mypy-unused-awaitable = false
```

## 並列実行

linters/testersは`jobs`で指定した並列数で実行される（既定: 4）。

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
# textlintをfastから除外
textlint-fast = false
```

カスタムコマンドも`fast = true`でfastエイリアスに追加できる。

## 出力順序

非TUIモード (`--no-ui`、`--ci`、または非対話端末) では、既定で全コマンドの完了後に成功コマンド詳細 → 失敗コマンド詳細 → `summary`の順でまとめて出力する。
`pyfltr ... | tail -N`のようにパイプで末尾だけ切り出してもsummaryと失敗情報が末尾に残るため、Claude Codeなど末尾だけを読み取るツールでも実行結果を把握できる。

従来の「各コマンドの完了時に即座に詳細ログを出す」挙動を使いたい場合は`--stream`を指定する。

---

個別のツール設定（2段階実行、ファイルパターン、直接実行 / js-runner / bin-runnerのカテゴリ別設定、カスタムコマンド等）の詳細は[ツール別設定](configuration-tools.md)を参照。
