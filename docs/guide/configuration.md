# 設定項目

`pyproject.toml`で設定する。

## 例

```toml
[tool.pyfltr]
preset = "latest"
pylint-args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

## グローバル設定

プロジェクトをまたいで共通にしたい設定（archiveの保持期間やキャッシュ保存期間など）は、
ユーザーレベルのグローバル設定ファイルに書くことでマシン単位で集約できる。

### グローバル設定ファイルのパス

OS別のパスは次の通り（`platformdirs.user_config_dir("pyfltr")`が解決する場所）。

- Linux: `~/.config/pyfltr/config.toml`
- macOS: `~/Library/Application Support/pyfltr/config.toml`
- Windows: `%APPDATA%\pyfltr\config.toml`

環境変数`PYFLTR_GLOBAL_CONFIG`を設定するとそのパスを優先する
（テスト容易性の確保やユーザーによる強制上書きを目的とした差し替え用。`PYFLTR_CACHE_DIR`と命名対称）。

### 書式

`pyproject.toml`と同じ形式で、`[tool.pyfltr]`セクション配下にキーを列挙する。

```toml
[tool.pyfltr]
archive-max-age-days = 30
archive-max-size-mb = 2048
cache-max-age-hours = 24
```

全項目をグローバル設定ファイルに書くことができる。
ただし、archive系とcache系の計6キーのみ特殊仕様で、グローバル設定が優先される。

- archive系（4キー）: `archive` / `archive-max-runs` / `archive-max-size-mb` / `archive-max-age-days`
- cache系（2キー）: `cache` / `cache-max-age-hours`

これらをproject側の`pyproject.toml`に書いた場合は警告が出る。
それ以外のキーはproject側が優先されるため、グローバル設定は未設定時のフォールバックとして機能する。

### 設定の適用順

1. デフォルト値を生成する
2. グローバル設定とproject設定（`pyproject.toml`）を読み込み、1つの入力にマージする
   - archive/cache系はマージ時にグローバル側を優先する（project側に同じキーがあっても上書きされる）
   - それ以外のキーは後勝ち（project側が優先）
3. マージ結果にプリセット（`preset`）を反映する
4. 言語カテゴリゲート（`python` / `javascript` / `rust` / `dotnet`）を適用する

適用優先度は`preset < 言語カテゴリゲート < 個別設定`。
`pyproject.toml`が存在しないディレクトリでもグローバル設定は反映される。

### 設定操作

`pyfltr config`サブコマンドを使うと、project側の`pyproject.toml`とglobal側の`config.toml`の
両方をCLIから直接操作できる。
`--global`の有無で対象ファイルを切り替える。
詳細は[CLIコマンド](usage.md#config)を参照。

## 設定項目一覧 {#config-keys}

設定項目と既定値は`pyfltr config list`で確認可能。
`{command}`系の項目およびツール固有の項目（`prettier-check-args`など）の詳細はツール別設定ページを参照。

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
- {command}-fix-args : fix段で`{command}-args`の後に追加する引数
 （既定値はtextlint / markdownlint / ruff-check / eslint / biomeのみ定義）
- {command}-targets : 対象ファイルパターンの完全上書き
- {command}-extend-targets : 対象ファイルパターンへの追加
- {command}-exclude : ツール別の追加除外パターン（後述）
- {command}-pass-filenames : ファイル引数をコマンドに渡すか否か（既定: `true`）
- {command}-runner : ツール起動方式。
  カテゴリ委譲値（`"python-runner"` / `"js-runner"` / `"bin-runner"`）または直接指定値（9種）の対称12値を許容する。
  既定値はツールごとに異なる（[ツール別設定](configuration-tools.md#command-runner)を参照）
- python-runner : `{command}-runner = "python-runner"`の解決先（`"uv"` / `"uvx"` / `"direct"`、既定: `"uv"`）
- js-runner : `{command}-runner = "js-runner"`の解決先（`"pnpx"` / `"pnpm"` / `"npm"` / `"npx"` / `"yarn"` / `"direct"`、既定: `"pnpx"`）
- bin-runner : `{command}-runner = "bin-runner"`の解決先（`"mise"` / `"direct"`、既定: `"mise"`）
- {command}-version : bin-runner対応ツールのバージョン指定（既定: `"latest"`）
- pylint-pydantic : pylint実行時に`--load-plugins=pylint_pydantic`を自動追加するか（既定: `true`、後述）
- mypy-unused-awaitable : mypy実行時に`--enable-error-code=unused-awaitable`を自動追加するか（既定: `true`、後述）
- jobs : linters/testersの最大並列数（既定: 4。CLIの`-j`オプションでも指定可能）
- exclude : 除外するファイル名/ディレクトリ名パターン（既定値あり）
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン（既定は空）
- respect-gitignore : `.gitignore`に記載されたファイルを除外するか否か（既定: `true`）。
  gitのルートおよびネストした`.gitignore`、グローバルgitignore、`.git/info/exclude`を全て考慮する。`git`コマンドが必要
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
- textlint-protected-identifiers : textlint fixで破損させてはならない識別子のリスト。
  既定値は`[".NET", "Node.js", "Vue.js", "Next.js", "Nuxt.js"]`。
  詳細は[ツール別設定](configuration-tools.md#textlint-protected-identifiers)を参照

`prettier-check-args` / `prettier-write-args` / `shfmt-check-args` / `shfmt-write-args`などの
2段階実行向け引数はツール別設定ページで詳しく扱う。

## プリセット設定 {#preset}

プリセットは各時点での推奨ツール構成をバージョン付きで示すスナップショット。
Python / JavaScript / TypeScript / Rust / .NET / ドキュメント系の推奨ツールを横断的に収録する。
`"latest"`または日付指定（`"20260413"` / `"20260411"` / `"20260330"`）を指定する。

```toml
[tool.pyfltr]
preset = "latest"
```

`preset = "latest"`はpyfltrの更新に伴って対象ツールの追加や既定値の変更が予告なく入ることがある。
破壊的変更を避けたい場合は日付指定プリセットで固定すると、当該日時点の構成をそのまま維持できる。

プリセットで`true`になっているツールも、次節の言語カテゴリキーがゲートを開けた言語分だけが実際に実行される。
`preset = "latest"` + `{language} = true`だけで当該言語の推奨ツール一式が有効化される運用を意図している。

### preset "20260413" / "latest"

以下の設定が行われる。

Python核（`python = true`で通過）

- `ruff-format = true`
- `ruff-check = true`
- `mypy = true`
- `pylint = true`
- `pyright = true`
- `pytest = true`
- `uv-sort = true`

JavaScript / TypeScript（`javascript = true`で通過）

- `eslint = true`
- `biome = true`
- `oxlint = true`
- `prettier = true`
- `tsc = true`
- `vitest = true`

Rust（`rust = true`で通過）

- `cargo-fmt = true`
- `cargo-clippy = true`
- `cargo-check = true`
- `cargo-test = true`
- `cargo-deny = true`

.NET（`dotnet = true`で通過）

- `dotnet-format = true`
- `dotnet-build = true`
- `dotnet-test = true`

ドキュメント系（カテゴリゲート非対象、常時通過）

- `textlint = true`
- `markdownlint = true`
- `actionlint = true`
- `typos = true`
- `pre-commit = true`

### preset "20260411"

`"20260413"`から`pre-commit = true`を除いた構成（pre-commitは`"20260413"`以降で追加）。
それ以外の有効化ツール（Python核・JavaScript / TypeScript・Rust・.NET・
textlint / markdownlint / actionlint / typos / uv-sort）は同一。

### preset "20260330"

`"20260411"`から`actionlint = true` / `typos = true` / `uv-sort = true`を除いた構成。
Python核・JavaScript / TypeScript・Rust・.NET・textlint・markdownlint・pyrightを有効化する。

## 言語カテゴリによるゲート制御

各言語カテゴリに属するツールは既定で無効（opt-in）。
プロジェクトで利用する言語カテゴリキーを`true`にすると、プリセットで推奨された当該言語ツールがゲートを通過して有効化される。
カテゴリキーを`false`（既定）にすると、プリセットで`true`になっていてもゲートで`false`へ押し戻される。

`preset = "latest"` + `{language} = true`の組み合わせだけで当該言語の推奨ツール一式が有効化される。

個別のツール単位では`{command} = true`での有効化・`{command} = false`での無効化も可能で、
適用優先度は`preset < 言語カテゴリゲート < 個別設定`。

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
Python系ツール一式は本体依存に同梱されているため、`uvx pyfltr`単発で利用できる。
JavaScript系・Rust系・.NET系は各言語のツールチェイン（Node.js・cargo・dotnet CLI）が前提となる。

対応するPython系ツールはruff-format / ruff-check / mypy / pylint / pyright / ty / pytest / uv-sortの8種。
このうちtyのみpreset非収録のため、必要に応じて個別に`ty = true`を指定する（ゲートを越えて最優先）。

```toml
[tool.pyfltr]
preset = "latest"
python = true
ty = true        # preset 非収録のため個別指定で追加
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

`pass-filenames = false`のツール（pre-commit・tsc・cargo-\*・dotnet-\*など）はファイル名をコマンドに渡さないため、
`{command}-exclude`を設定しても効果がない。

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

非TUIモード（`--no-ui`、`--ci`、または非対話端末）では、既定で全コマンドの完了後に
成功コマンド詳細 → 失敗コマンド詳細 → `summary`の順でまとめて出力する。
`pyfltr ... | tail -N`のようにパイプで末尾だけ切り出してもsummaryと失敗情報が末尾に残るため、
Claude Codeなど末尾だけを読み取るツールでも実行結果を把握できる。

従来の「各コマンドの完了時に即座に詳細ログを出力する」挙動を使いたい場合は`--stream`を指定する。

---

個別のツール設定（2段階実行、ファイルパターン、直接実行 / js-runner / bin-runnerのカテゴリ別設定、
`mise-auto-trust`によるmise未信頼の自動対応、カスタムコマンド等）の詳細は[ツール別設定](configuration-tools.md)を参照。
