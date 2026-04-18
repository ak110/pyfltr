# 設定項目（ツール別）

基本設定（プリセット、Python一括有効化、並列実行等）は[設定項目](configuration.md)を参照。

pyfltrは対応ツールを実行方式の観点から次の3カテゴリに分けて扱う。

- 直接実行: Python系ツール、Rust系（cargo系）、.NET系（dotnet系）など。PATH上または`{command}-path`で指定した実行ファイルを直接呼び出す
- js-runner経由: eslint / prettier / biome / oxlint / tsc / vitest / markdownlint-cli2 / textlintなど。npm / pnpm / yarn等のJavaScriptパッケージマネージャー経由で起動する
- bin-runner経由: ec / shellcheck / shfmt / typos / actionlintなど。miseやPATH経由でネイティブバイナリを解決する

本ページではまず全カテゴリ共通の設定を示し、続いて各カテゴリ固有の設定を説明する。

## 対象ファイルパターンのカスタマイズ

各コマンドが処理する対象ファイルパターンを変更できる。

`{command}-targets`でパターンを完全に上書きする。

```toml
[tool.pyfltr]
# shfmtの対象を *.bash のみに変更（既定の *.sh は対象外になる）
shfmt-targets = ["*.bash"]
```

`{command}-extend-targets`で既存パターンに追加する。

```toml
[tool.pyfltr]
# shfmtの対象に *.sh.tmpl と dot_bashrc を追加（既定の *.sh も維持）
shfmt-extend-targets = ["*.sh.tmpl", "dot_bashrc"]
shellcheck-extend-targets = ["*.sh.tmpl", "dot_bashrc"]
```

両方を指定した場合、`targets`で上書きした後に`extend-targets`で追加する。

## pass-filenames設定

`{command}-pass-filenames = false`を設定すると、コマンド実行時にファイル引数を渡さない。
プロジェクト全体を一括チェックするツール（`tsc`など）で使用する。

ビルトインでは、プロジェクト全体を単位として動作する以下のツールが`pass-filenames = false`に設定されている。

- `tsc`
- `pre-commit`
- `cargo-fmt` / `cargo-clippy` / `cargo-check` / `cargo-test` / `cargo-deny`
- `dotnet-format` / `dotnet-build` / `dotnet-test`

カスタムコマンドでも同様に設定可能。

```toml
[tool.pyfltr]
tsc = true
# tsc は既定で pass-filenames = false のため明示不要

[tool.pyfltr.custom-commands.commitlint]
type = "linter"
path = "commitlint"
args = ["--from=HEAD~1"]
pass-filenames = false
```

## 直接実行ツール

Python系ツール、Rust系（cargo系）、.NET系（dotnet系）、uv-sortはいずれも`{command}-path`で指定した実行ファイルをpyfltrが直接呼び出す。
特別な起動経路を挟まないため、miseやnpm等のランナー設定は不要。

### ruff-format の 2 段階実行

`ruff-format` は既定で `ruff check --fix --unsafe-fixes` と `ruff format` の2ステップを連続実行する。
importソートや自動修正可能なlint違反を整形と同時に処理するための挙動。

ステップ1のlint違反（ruff checkのexit 1）は無視され、別途 `ruff-check` コマンドで検出される想定。
設定ミス等によるruffの異常終了（exit 2以上）は失敗と判定する。

ステップ1に`--unsafe-fixes`を既定で含めているのは意図的な設計である。
`--unsafe-fixes`を外すと自動修正できない違反が増え、手動対処の手間が増加する。
実運用ではバージョン管理下で作業するため自動修正は容易に取り消せ、かつ`--unsafe-fixes`が実害のある修正を生むケースはまれである。
このため開発体験を優先して既定で有効にしている。保守的に運用したい場合は後述のとおり`ruff-format-check-args`で上書きする。

fix段で使う`ruff-check-fix-args`（既定値`["--fix", "--unsafe-fixes"]`）も同じ方針で`--unsafe-fixes`を含めている。

```toml
[tool.pyfltr]
# ステップ 1 をスキップしたい場合 (既定は true)
ruff-format-by-check = false
# ステップ 1 の引数を差し替えたい場合 (既定は ["check", "--fix", "--unsafe-fixes"])
ruff-format-check-args = ["check", "--fix"]
```

### uv-sort

`uv-sort`は`pyproject.toml`の依存定義をソートするformatter。既定で無効。

```toml
[tool.pyfltr]
uv-sort = true
```

Python系ツールとして扱われ、`python = true`のゲート対象となる（プリセット`20260411`以降に含まれる）。
`mypy` / `pylint` / `pytest`も全プリセットに含まれ、`python = true`だけでゲートを通過する。

### Rust系（cargo系）

`cargo-fmt` / `cargo-clippy` / `cargo-check` / `cargo-test` / `cargo-deny`はいずれも既定で無効（opt-in）。
全プリセットにRust系ツールが含まれるため、`preset = "latest"` + `rust = true`だけで一式が有効化される。
`pass-filenames = false`によりcrate全体を対象とするproject-level実行となる。

```toml
[tool.pyfltr]
preset = "latest"
rust = true
```

実行パスはツール別に個別指定する。
既定ではcargo系は`cargo`バイナリ経由、cargo-denyのみ`cargo-deny`バイナリを直接呼び出す。

```toml
[tool.pyfltr]
# miseやrustup等で別のcargoを指すようにしたい場合
cargo-fmt-path = "/path/to/cargo"
cargo-clippy-path = "/path/to/cargo"
cargo-check-path = "/path/to/cargo"
cargo-test-path = "/path/to/cargo"
cargo-deny-path = "/path/to/cargo-deny"
```

既定の引数は以下のとおり。必要に応じて上書きできる。

- cargo-fmt: `cargo-fmt-args = ["fmt"]`（常時書き込みモード。pyfltr規約によりformatterは`--fix`なしでも強制修正する）
- cargo-clippy:
    - `cargo-clippy-args = ["clippy", "--all-targets"]`（共通前半部）
    - `cargo-clippy-lint-args = ["--", "-D", "warnings"]`（lintモードで末尾に付与）
    - `cargo-clippy-fix-args = ["--fix", "--allow-staged", "--allow-dirty", "--", "-D", "warnings"]`（fixモードで末尾に付与）
- cargo-check: `cargo-check-args = ["check", "--all-targets"]`
- cargo-test: `cargo-test-args = ["test"]`
- cargo-deny: `cargo-deny-args = ["check"]`

cargo系はcrate単位で同一ワークスペースを操作するため、`serial_group = "cargo"`で自動的に直列化される。
利用者が`--jobs=1`などを指定する必要はない。

### .NET系（dotnet系）

`dotnet-format` / `dotnet-build` / `dotnet-test`はいずれも既定で無効（opt-in）。
全プリセットに`.NET`系ツールが含まれるため、`preset = "latest"` + `dotnet = true`だけで一式が有効化される。
`pass-filenames = false`によりsolution全体を対象とするproject-level実行となる。

```toml
[tool.pyfltr]
preset = "latest"
dotnet = true
```

実行パスはツール別に個別指定する。既定はいずれも`dotnet`バイナリ。

```toml
[tool.pyfltr]
dotnet-format-path = "/path/to/dotnet"
dotnet-build-path = "/path/to/dotnet"
dotnet-test-path = "/path/to/dotnet"
```

既定の引数は以下のとおり。

- dotnet-format: `dotnet-format-args = ["format"]`（常時書き込みモード）
- dotnet-build: `dotnet-build-args = ["build", "--nologo"]`
- dotnet-test: `dotnet-test-args = ["test", "--nologo"]`

## js-runner経由で実行するツール

markdownlint-cli2 / textlint / eslint / prettier / biome / oxlint / tsc / vitestは`js-runner`設定で起動方式を切り替える。
既定は`pnpx`で、グローバル/キャッシュから都度取得する挙動となる。

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

`{command}-path`を明示的に設定した場合はその値が優先され、自動解決は無効化される。
グローバルインストール済みのtextlintを直接使いたい場合などに利用する。

```toml
[tool.pyfltr]
textlint-path = "textlint"
# 共通引数 (lint/fix 両モードで付与)
textlint-args = []
# lint モード専用の引数 (既定で --format compact。builtin パーサが compact 出力を想定している)
textlint-lint-args = ["--format", "compact"]
```

textlintのfix実行 (`textlint --fix`) では `@textlint/fixer-formatter` が使われ、`compact` フォーマッタを解決できない。
このため `--format compact` は `textlint-args`（共通）ではなく `textlint-lint-args`（lintモード専用）に分離している。

fix段では、pyfltrはtextlintを2段階で実行する（fix適用 → lintチェック）ため、
残存違反はcompact形式で正しく取得される。
旧版から `textlint-args = ["--format", "compact", ...]` の設定を引き継いでいる場合でも、
pyfltrはfixステップの起動コマンドから `--format` ペアを自動除去するためクラッシュしない。
新規設定では `textlint-lint-args` に書くことを推奨する。

### prettier の 2 段階実行

`prettier`は`--check`（読み取り専用）と`--write`（書き込み）が排他のため、pyfltrは2段階で実行する。

まず`prettier --check`を実行する。

- `rc == 0` → `succeeded`（整形済み）
- `rc == 1` → 続けて`prettier --write`を実行し、`rc == 0`なら`formatted`、それ以外は`failed`
- `rc >= 2` → `failed`（設定ミス等の致命的エラー、`--write`は実行しない）

引数は`prettier-check-args` / `prettier-write-args`で個別に上書きできる（既定はそれぞれ`["--check"]` / `["--write"]`）。
共通引数`prettier-args`は両ステップの先頭に付与される。

```toml
[tool.pyfltr]
prettier = true
# キャッシュ等の共通引数
prettier-args = ["--cache"]
```

### textlintのプリセット/ルール指定

textlintで利用するルール/プリセットパッケージは`textlint-packages`に列挙する。
既定では以下の3パッケージが含まれる。

```toml
[tool.pyfltr]
textlint-packages = [
    "textlint-rule-preset-ja-technical-writing",
    "textlint-rule-preset-jtf-style",
    "textlint-rule-ja-no-abusage",
]
```

`textlint-packages`は`pnpx` / `npx`モード時に`--package` / `-p`展開される。
`pnpm` / `npm` / `yarn` / `direct`モードでは`package.json`側でインストールする前提のため無視される。

#### 保護対象の識別子の破損検知 {#textlint-protected-identifiers}

`preset-jtf-style`の「半角ピリオド→全角句点」変換などが、コードブロック外にある`.NET` / `Node.js`などの識別子まで破損させることがある。
pyfltrは`textlint --fix`のステップ直後に「fix前には含まれていた保護対象の識別子がfix後に減っていないか」を検査し、破損の疑いがあれば`textlint-identifier-corruption`ソースの警告を発行する。
識別子リストは`textlint-protected-identifiers`で上書きでき、空リスト`[]`を指定すると検知を無効化できる。
警告は注意喚起のみで、ツールの成否判定には影響しない。

```toml
[tool.pyfltr]
textlint-protected-identifiers = [".NET", "Node.js", "Vue.js", "Next.js", "Nuxt.js"]
```

### eslint / prettier / biomeの設定

eslint / prettier / biomeはすべて既定で無効（opt-in）。
全プリセットにJS/TS系ツールが含まれるため、`preset = "latest"` + `javascript = true`だけで一式が有効化される。
プラグインは`package.json`管理が前提のため、通常は`js-runner = "pnpm"`と併用する。

```toml
[tool.pyfltr]
preset = "latest"
js-runner = "pnpm"
javascript = true
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

### oxlint / tsc / vitest

oxlint / tsc / vitestもjs-runner対応のツール。すべて既定で無効（opt-in）。
全プリセットに含まれるため、`preset = "latest"` + `javascript = true`でeslint / prettier / biomeと同時に一式が有効化される。

```toml
[tool.pyfltr]
preset = "latest"
js-runner = "pnpm"
javascript = true
```

既定の引数は以下のとおり。

- oxlint: `oxlint-args = []`
- tsc: `tsc-args = ["--noEmit"]`、`tsc-pass-filenames = false`（プロジェクト全体をチェックするためファイル引数を渡さない）
- vitest: `vitest-args = ["run"]`（`run`サブコマンドが必須）

個別に無効化したい場合のみ`{command} = false`を指定する。

## bin-runner経由で実行するツール

ec / shellcheck / shfmt / typos / actionlintはネイティブバイナリ（Go/Rust/Haskell製等）で、`bin-runner`設定で起動方式を切り替える。
ecはeditorconfig-checkerの略称。既定は`mise`で、[mise](https://mise.jdx.dev/)によるバージョン管理付きの実行となる。

```toml
[tool.pyfltr]
# mise経由で実行する（既定）
bin-runner = "mise"
```

| `bin-runner` | 挙動 |
| --- | --- |
| `mise` | `mise exec <tool>@<version> -- <command>`で起動する（既定）。ツールの自動インストールにも対応 |
| `direct` | PATH上のバイナリを直接実行する |

ツールが見つからない場合はエラー扱い（`failed`）となる。
`mise`モードの場合、実行環境にmiseがインストールされている必要がある。

### CIでの設定

GitHub ActionsでCIを実行する場合は[jdx/mise-action](https://github.com/jdx/mise-action)でmiseをセットアップする。

```yaml
- name: Setup mise
  uses: jdx/mise-action@v4
```

miseを使わず、PATH上のバイナリを直接使う場合は`bin-runner = "direct"`を設定する。

### バージョン指定

`{command}-version`でbin-runner対応ツールのバージョンを指定できる。既定は`"latest"`。

```toml
[tool.pyfltr]
shellcheck-version = "0.10.0"
shfmt-version = "3.10.0"
```

miseモードでは`mise exec <tool>@<version> -- <command>`として展開される。directモードではバージョン指定は無視される。

### bin-runner対応ツールの設定

各ツールはすべて既定で無効。有効化には`pyproject.toml`で切り替える。

```toml
[tool.pyfltr]
ec = true
shellcheck = true
shfmt = true
typos = true
actionlint = true
```

既定の引数は以下のとおり。必要に応じて上書きできる。

- ec: `ec-args = ["-format", "gcc", "-no-color"]`
- shellcheck: `shellcheck-args = ["-f", "gcc"]`
- shfmt: `shfmt-check-args = ["-l"]` / `shfmt-write-args = ["-w"]`（2段階実行。共通引数は`shfmt-args`で指定）
- typos: `typos-args = ["--format", "brief"]`
- actionlint: `actionlint-args = []`

`{command}-path`を明示的に設定した場合はその値が優先され、bin-runnerによる自動解決は無効化される。

### shfmtの2段階実行

`shfmt`はprettierと同様に2段階で実行する。

まず`shfmt -l`（チェックのみ）を実行する。

- `rc == 0` → `succeeded`（整形済み）
- `rc != 0` → 続けて`shfmt -w`（書き込み）を実行する

引数は`shfmt-check-args` / `shfmt-write-args`で個別に上書きできる（既定はそれぞれ`["-l"]` / `["-w"]`）。
共通引数`shfmt-args`は両ステップの先頭に付与される。

```toml
[tool.pyfltr]
shfmt = true
shfmt-args = ["-i", "2"]
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
config-files = [".banditrc", "pyproject.toml"]
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
- `fast`: `fast`サブコマンドに含めるか否か（省略時は`false`）
- `fix-args`: fix段で`args`の後ろへ追加する引数（省略時はfix段の対象外）
- `pass-filenames`: ファイル引数をコマンドに渡すか否か（省略時は`true`）。プロジェクト全体を一括チェックするツールでは`false`に設定する
- `config-files`: このコマンドの設定ファイル候補のリスト（省略時は空）。globパターン可。有効化時にどれもプロジェクトルート直下に見つからないとpyfltrが警告を出す（ツール自体は実行する）。pre-commitなどの「設定ファイル無しでは機能しないツール」の設定不備を可視化する用途

ビルトインコマンド（mypy等）は自動的にエラーパースされる。
カスタムコマンドに対しても`--{name}-args`やenable/disableを使用できる。

`pyfltr run` / `pyfltr ci` のように`--commands`を省略したサブコマンドでは、登録済みの有効なカスタムコマンドもビルトインと同様にデフォルトの実行対象に含まれる。
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
