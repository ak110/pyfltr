# 設定項目（ツール別）

基本設定（プリセット、Python一括有効化、並列実行等）は[設定項目](configuration.md)を参照。

pyfltrは対応ツールを実行方式の観点から次の3カテゴリに分けて扱う。
カテゴリの選択はツールごとの`{command}-runner`設定で切り替え可能で、既定値はカテゴリに対応した値になっている。

- 直接実行（既定`{command}-runner = "direct"`）。
  対象はPython系ツール（mypy / pylint / pyright / ty / pytest / ruff-format / ruff-check / uv-sort）。
  これにtypos、yamllint、pre-commitも加わる。
  PATH上または`{command}-path`で指定した実行ファイルを直接呼び出す
- js-runner経由（既定`{command}-runner = "js-runner"`）。
  対象はeslint / prettier / biome / oxlint / tsc / vitest / markdownlint-cli2 / textlint。
  npm / pnpm / yarn等のJavaScriptパッケージマネージャー経由で起動する
- bin-runner経由（既定`{command}-runner = "bin-runner"`）。
  対象は既存のec / shellcheck / shfmt / actionlint / glab-ci-lint / taplo / hadolint / gitleaks。
  さらにcargo系（cargo-fmt / cargo-clippy / cargo-check / cargo-test / cargo-deny）も含む。
  加えてdotnet系（dotnet-format / dotnet-build / dotnet-test）も対象。
  グローバル`bin-runner`設定（既定`mise`）に従ってmiseまたはPATH経由でネイティブバイナリを解決する

`{command}-runner`の許容値は`"direct"` / `"mise"` / `"bin-runner"` / `"js-runner"`の4種。
個別ツール単位で起動方式を切り替えたい場合は`{command}-runner`を明示する
（例: `cargo-fmt-runner = "direct"`でcargoをPATH経由で直接呼び出す形に戻せる）。

`{command}-path`を非空に指定するとあらゆる`{command}-runner`設定より優先され、その値で直接実行する。
旧版から個別パスを指定して使っている場合の後方互換経路として機能する。

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

対象はPython系ツール（mypy / pylint / pyright / ty / pytest / ruff-format / ruff-check / uv-sort）。
これにtypos、yamllint、pre-commitを加えた一群が直接実行カテゴリ。
いずれも`{command}-runner`既定値は`"direct"`で、`{command}-path`で指定した実行ファイルをpyfltrが直接呼び出す。
特別な起動経路を挟まないため、miseやnpm等のランナー設定は不要。

### ruff-format の 2 段階実行

`ruff-format` は既定で `ruff check --fix --unsafe-fixes` と `ruff format` の2ステップを連続実行する。
importソートや自動修正可能なlint違反を整形と同時に処理するための挙動。

ステップ1のlint違反（ruff checkのexit 1）は無視され、別途 `ruff-check` コマンドで検出される想定。
設定ミス等によるruffの異常終了（exit 2以上）は失敗と判定する。

ステップ1に`--unsafe-fixes`を既定で含めているのは意図的な設計である。
`--unsafe-fixes`を外すと自動修正できない違反が増え、手動対処の手間が増加する。
実運用ではバージョン管理下で作業するため自動修正は容易に取り消せ、
かつ`--unsafe-fixes`が実害のある修正を生むケースはまれである。
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

### typos

`typos`はスペルチェッカー。PyPI経由でインストールされるため、`uv add pyfltr`だけで利用可能。

```toml
[tool.pyfltr]
typos = true
```

既定の引数は`typos-args = ["--format", "brief"]`。
`typos-path`を変更したい場合は明示的に指定する。

```toml
[tool.pyfltr]
typos-path = "/path/to/typos"
```

プロジェクト固有の許可語がある場合は`[tool.typos]`セクションも追記する
（詳細は[推奨設定例](recommended.md)の「typosの許可語設定」を参照）。

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

`preset-jtf-style`の「半角ピリオド→全角句点」変換などが、コードブロック外にある
`.NET` / `Node.js`などの識別子まで破損させることがある。
pyfltrは`textlint --fix`のステップ直後に識別子の減少を検査する。
破損の疑いがあれば`textlint-identifier-corruption`ソースの警告を発行する。
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
    - 注: `biome-args`の先頭からサブコマンド（`check` / `lint` / `format`）を外すとbiomeがhelp表示で失敗する。
      必ずサブコマンド名を残すこと

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
- vitest: `vitest-args = ["run", "--passWithNoTests"]`（`run`サブコマンドが必須、filter結果ゼロ時もsuccess扱い）

個別に無効化したい場合のみ`{command} = false`を指定する。

## bin-runner経由で実行するツール

以下のネイティブバイナリ系ツールはグローバル`bin-runner`設定で起動方式を切り替える。

- ec / shellcheck / shfmt / actionlint / glab-ci-lint / taplo / hadolint / gitleaks
- cargo系（cargo-fmt / cargo-clippy / cargo-check / cargo-test / cargo-deny）
- dotnet系（dotnet-format / dotnet-build / dotnet-test）

ecはeditorconfig-checkerの略称。既定は`mise`で、[mise](https://mise.jdx.dev/)によるバージョン管理付きの実行となる。
cargo系・dotnet系はv3.x系で`{command}-path`既定値を空文字に変更し、本カテゴリへ統合した（破壊的変更）。
従来挙動（PATH上の`cargo` / `dotnet` / `cargo-deny`を直接実行）を維持したい場合は次の2方法のいずれかを使う。
個別ツールに`{command}-runner = "direct"`を設定するか、`{command}-path`に明示的なパスを指定する。

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

`mise`モードでも、miseバイナリ自体がPATH上に存在しない場合に限り`direct`相当のPATH解決へ静かにフォールバックする。
ディストロパッケージで`shellcheck`等のネイティブバイナリだけを導入した環境でも追加設定なしで動作させるための救済挙動である。

ただし、miseは存在するが`mise exec`が失敗する場合（バージョン解決失敗・config未信頼など）はフォールバックせず、
従来どおり`failed`扱いとなる。

### CIでの設定

GitHub ActionsでCIを実行する場合は[jdx/mise-action](https://github.com/jdx/mise-action)でmiseをセットアップする。

```yaml
- name: Setup mise
  uses: jdx/mise-action@v4
```

miseを使わず、PATH上のバイナリを直接使う場合は`bin-runner = "direct"`を設定する。

### mise-auto-trust

`mise`モードでは、worktreeやdotfiles配下のディレクトリなど、`mise.toml`が未信頼扱いになっている場合に
事前チェックが失敗することがある。
既定では`mise-auto-trust = true`となっており、未信頼configを検出すると`mise trust --yes --all`を自動実行して
信頼を確立してからリトライする。
`--all`オプションはcwdおよびその親ディレクトリにある全configを対象とするため、
プロジェクト外の親ディレクトリに`mise.toml`が存在する場合もまとめて信頼される点に注意する。

`mise-auto-trust`が不要な場合は無効化できる。

```toml
[tool.pyfltr]
mise-auto-trust = false
```

無効化した場合、未信頼configが原因の失敗はmise由来のエラーメッセージとともに`failed`扱いとなる。
手動で`mise trust`を実行して対処する。

### バージョン指定

`{command}-version`でbin-runner対応ツールのバージョンを指定できる。既定は`"latest"`。
cargo系・dotnet系も同枠組みで指定できる。

```toml
[tool.pyfltr]
shellcheck-version = "0.10.0"
shfmt-version = "3.10.0"
cargo-fmt-version = "1.83.0"
dotnet-format-version = "9.0.100"
```

miseモードでは`mise exec <tool>@<version> -- <command>`として展開される。directモードではバージョン指定は無視される。

### {command}-runnerによる個別ツール切替 {#command-runner}

ツール単位で起動方式を変更したいときは`{command}-runner`を指定する。許容値は次の4種。

- `"direct"`: PATH上または`{command}-path`で指定した実行ファイルを直接呼び出す
- `"mise"`: `mise exec <backend>@<version> -- <bin>`形式で起動する
- `"bin-runner"`: グローバル`bin-runner`設定（`mise` / `direct`）に委譲する
- `"js-runner"`: グローバル`js-runner`設定（`pnpx` / `pnpm` / `npm` / `npx` / `yarn` / `direct`）に委譲する

`"mise"`を明示できるのは本カテゴリ（既存8ツール + cargo系 + dotnet系）に限る。
typos / yamllint / Python系ツールなどbackend未登録のツールに`"mise"`を明示すると、設定読み込み時または
解決時に明確なエラーとして拒否する（typo・誤設定の早期検知のため）。

```toml
[tool.pyfltr]
# cargo系をPATH直接実行に戻す
cargo-fmt-runner = "direct"
cargo-clippy-runner = "direct"
# 個別ツールだけmise経由（グローバルはdirectのまま）
shellcheck-runner = "mise"
```

direct実行時、対象が`dotnet`バイナリの場合は環境変数`DOTNET_ROOT`配下の`dotnet`実行ファイルを優先採用する
（PATHに`dotnet`が無くてもSDK配置場所で起動できる運用を救済するため）。
miseモードでは`DOTNET_ROOT`は参照せず、miseが管理する環境に委ねる。

### bin-runner対応ツールの設定

各ツールはすべて既定で無効。有効化には`pyproject.toml`で切り替える。

```toml
[tool.pyfltr]
ec = true
shellcheck = true
shfmt = true
actionlint = true
glab-ci-lint = true
taplo = true
hadolint = true
gitleaks = true
cargo-fmt = true
cargo-clippy = true
cargo-check = true
cargo-test = true
cargo-deny = true
dotnet-format = true
dotnet-build = true
dotnet-test = true
```

既定の引数は以下のとおり。必要に応じて上書きできる。

- ec: `ec-args = ["-format", "gcc", "-no-color"]`
- shellcheck: `shellcheck-args = ["-f", "gcc"]`
- shfmt: `shfmt-check-args = ["-l"]` / `shfmt-write-args = ["-w"]`（2段階実行。共通引数は`shfmt-args`で指定）
- actionlint: `actionlint-args = []`
- glab-ci-lint: `glab-ci-lint-args = ["ci", "lint"]`（`glab ci lint`サブコマンドを既定値として保持）。
  GitLabリモートが未登録または未認証の環境では`glab`自身がエラー終了するため、pyfltrが自動でスキップ扱いに変換する
- taplo: `taplo-check-args = ["check"]` / `taplo-write-args = ["format"]`（2段階実行。共通引数は`taplo-args`で指定）
- hadolint: `hadolint-args = []`（Dockerfileを対象とするため`targets`に`Dockerfile`・`Dockerfile.*`・`*.Dockerfile`を含む）
- gitleaks: `gitleaks-args = ["detect", "--no-banner"]`（`detect`サブコマンドを既定値として保持）。
  `pass-filenames = false`によりリポジトリ全体を対象とする
- cargo-fmt: `cargo-fmt-args = ["fmt"]`（常時書き込みモード。pyfltr規約によりformatterは`--fix`なしでも強制修正する）
- cargo-clippy:
    - `cargo-clippy-args = ["clippy", "--all-targets"]`（共通前半部）
    - `cargo-clippy-lint-args = ["--", "-D", "warnings"]`（lintモードで末尾に付与）
    - `cargo-clippy-fix-args = ["--fix", "--allow-staged", "--allow-dirty", "--", "-D", "warnings"]`（fixモードで末尾に付与）
- cargo-check: `cargo-check-args = ["check", "--all-targets"]`
- cargo-test: `cargo-test-args = ["test"]`
- cargo-deny: `cargo-deny-args = ["check"]`
- dotnet-format: `dotnet-format-args = ["format"]`（常時書き込みモード）
- dotnet-build: `dotnet-build-args = ["build", "--nologo"]`
- dotnet-test: `dotnet-test-args = ["test", "--nologo"]`

cargo系・dotnet系はそれぞれ`serial_group = "cargo"` / `serial_group = "dotnet"`で自動的に直列化される
（同一ワークスペース・solutionを操作する競合を避けるため）。利用者が`--jobs=1`などを指定する必要はない。
mise backendは既定でcargo系が`rust`、cargo-denyが`cargo-deny`、dotnet系が`dotnet`に設定されている。

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

### taploの2段階実行

`taplo`はshfmtと同様に2段階で実行する。

まず`taplo check`（チェックのみ）を実行する。

- `rc == 0` → `succeeded`（整形済み）
- `rc != 0` → 続けて`taplo format`（書き込み）を実行する

引数は`taplo-check-args` / `taplo-write-args`で個別に上書きできる（既定はそれぞれ`["check"]` / `["format"]`）。
共通引数`taplo-args`は両ステップの先頭に付与される。

```toml
[tool.pyfltr]
taplo = true
taplo-args = ["--config", "taplo.toml"]
```

### ec（editorconfig-checker） {#ec}

`ec`は`.editorconfig`違反を検出するGo製のチェッカー。bin-runner経由で実行する。
プロジェクト固有の除外設定は`.editorconfig-checker.json`で行う。
設定キーはPascalCase、`Exclude`は正規表現の配列で指定する。

```json
{
  "Verbose": false,
  "Disable": {
    "IndentSize": true
  },
  "Exclude": [
    "\\.min\\.(js|css)$",
    "^vendor/",
    "^docs/_build/"
  ]
}
```

設定キーの全一覧は[editorconfig-checker公式ドキュメント](https://github.com/editorconfig-checker/editorconfig-checker#excluding-files)を参照する。

### yamllint

`yamllint`はPython製のYAMLリンター。直接実行経路（PATH上または`yamllint-path`で指定した実行ファイル）で動作する。
actionlintの`.github/workflows/*.yaml`対象とは独立して、YAML全般を対象とする。

```toml
[tool.pyfltr]
yamllint = true
# 設定ファイルを指定したい場合
yamllint-args = ["-c", ".yamllint.yml"]
```

既定では`yamllint`コマンドを直接呼び出す。パスを変更したい場合は`yamllint-path`を指定する。

```toml
[tool.pyfltr]
yamllint-path = "/path/to/yamllint"
```

### hadolint

`hadolint`はHaskell製のDockerfileリンター。bin-runner経由で実行する。
既定の対象ファイルは`Dockerfile` / `Dockerfile.*` / `*.Dockerfile`。
対象パターンを変更したい場合は`hadolint-targets`で上書きするか`hadolint-extend-targets`で追加する。

```toml
[tool.pyfltr]
hadolint = true
# 対象パターンを追加したい場合
hadolint-extend-targets = ["*.dockerfile"]
```

### gitleaks

`gitleaks`はGoバイナリのシークレット検出ツール。bin-runner経由で実行する。
`pass-filenames = false`により、ファイル一覧ではなくリポジトリ全体を対象として`gitleaks detect`を実行する。

```toml
[tool.pyfltr]
gitleaks = true
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
- `pass-filenames`: ファイル引数をコマンドに渡すか否か（省略時は`true`）。
  プロジェクト全体を一括チェックするツールでは`false`に設定する
- `config-files`: このコマンドの設定ファイル候補のリスト（省略時は空）。globパターン可。
  有効化時にどれもプロジェクトルート直下に見つからないとpyfltrが警告を出す（ツール自体は実行する）。
  pre-commitなどの「設定ファイル無しでは機能しないツール」の設定不備を可視化する用途

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

## command-info サブコマンド

`pyfltr command-info <tool>`は、対象ツールの起動方式（runner種別・実行ファイルパス・最終コマンドライン）の解決結果を
副作用無しで表示する。`pyproject.toml`の`{command}-runner`設定や`bin-runner` / `js-runner`の影響を実環境で確認したい
ときに使う。

```console
$ pyfltr command-info cargo-fmt
command: cargo-fmt
enabled: True
runner: bin-runner (default)
effective_runner: mise
executable: mise
executable_resolved: /home/user/.local/bin/mise
commandline: mise exec rust@latest -- cargo
configured_args: fmt
version: latest
```

主要なオプション。

- `--format=text|json`: 出力形式を切り替える（既定`text`）。json形式はスクリプトからのパース向け
- `--check`: mise経由ツールに対して`mise exec --version`での事前チェックを行う
 （`mise install` / `mise trust`が発火する場合があるため、既定では行わない）

未知のコマンド名や`{command}-runner = "mise"`を未登録ツールに指定した場合などは終了コード1で失敗する。
