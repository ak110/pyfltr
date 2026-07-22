# 推奨設定例（非Pythonプロジェクト）

TypeScript/JS・Rust・.NETプロジェクト向けの推奨構成例。
設定して実行するところから始める場合は[はじめに](getting-started.md)を参照。

共通のポイントは以下のとおり。

- `preset = "latest"`: 各時点での推奨ツール構成。
  ドキュメント系（textlint / markdownlint / actionlint / typos / pre-commit）は言語カテゴリゲートに属さず、
  プリセットでtrueになっているツールがそのまま有効化される
- 言語カテゴリゲートの詳細は[設定項目](configuration.md)を参照
- `uvx pyfltr`: pyfltrをdev依存に含めないため、`uvx`で都度取得して実行する
- 言語固有のツール + ドキュメント系lint（textlint / markdownlint / prettier）を組み合わせる
    - textlint / markdownlintの設定例（`.textlintrc.yaml`・`.markdownlint-cli2.yaml`）は
      [推奨設定例](recommended.md)を参照
- `bin-runner`の既定は`"mise"`。actionlint等のネイティブバイナリツールはmise経由で呼び出されるため、mise導入を推奨
- 個別ツールをPATH直接実行へ戻すには`{command}-runner = "direct"`または`{command}-path`を指定する
 （[ツール別設定](configuration-tools.md#command-runner)を参照）
- タスクランナー（Makefile / mise.toml）の設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照

## TypeScript/JS専用プロジェクト

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
preset = "latest"
javascript = true
js-runner = "pnpm"

extend-exclude = [
    ".svelte-kit",
    "node_modules",
    "dist",
    "build",
]
```

`.pre-commit-config.yaml`:

```yaml
  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uvx pyfltr fast
        types_or: [javascript, jsx, ts, tsx, json, css, yaml, markdown]
        require_serial: true
        language: system
```

ポイント:

- `javascript = true`: JS/TS系ツール一式（eslint / biome / oxlint / prettier / tsc / vitest）が
  プリセットのゲートを通過して有効化される
- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ
    - `textlint-packages`は無視される
- eslintとoxlintを併用すると、eslint非対応のルールをoxlintが補完できる（oxlintはRust製で高速）
- tsc: TypeScript型チェックを実行する
    - svelte-checkなどフレームワーク固有のチェッカーと併用する場合は、いずれか一方のみ有効化する
- vitest: `vitest-args = ["run", "--passWithNoTests"]`が既定のため追加引数を指定する必要はない
- 使わないツールは個別に`{command} = false`で無効化できる
- svelte-checkなどフレームワーク固有のツールはカスタムコマンドで追加する
 （[カスタムコマンド例](custom-commands.md)の「svelte-check」を参照）
- 依存の脆弱性監査は任意で追加できる（既定無効）。
  `js-runner`に合わせて`pnpm-audit` / `npm-audit` / `yarn-audit`のいずれかを有効化する

```toml
[tool.pyfltr]
pnpm-audit = true
# ネットワーク不調時に失敗ではなく警告として扱う場合
# pnpm-audit-severity = "warning"
```

`pnpm audit` / `npm audit` / `yarn audit`が`package.json`を対象にJavaScript依存の既知脆弱性を検査する。
外部脆弱性データベースへ問い合わせるためネットワーク接続が必須で結果が変動する。
yarn berry（2+）利用時は`yarn-audit-args = ["npm", "audit", "--json"]`へ上書きする。

監査結果はコード変更と無関係に変動するため、コミット毎ではなく定期実行に向く。
監査ツールのみをまとめて実行する場合は`--commands=audit`を指定し、`schedule`トリガーの専用ワークフローへ切り出す構成を推奨する。
SARIF出力と`github/codeql-action/upload-sarif`を組み合わせると、
同一の脆弱性が1件のアラートに集約され、重複通知を回避できる。

## Rustプロジェクト

`cargo fmt` / `cargo clippy` / `cargo test` / `cargo deny`と、
ドキュメント系lint（`textlint` / `markdownlint-cli2` / `prettier`）をpyfltrに一元化する例。
言語カテゴリはすべてopt-inのため、非Rustプロジェクトでcargo系が実行されることはない。

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
preset = "latest"
rust = true
js-runner = "pnpm"
# prettier はドキュメント系を pnpm で実行するために個別に opt-in する
# (javascript ゲートは開けず、Rust 専用プロジェクトで不要な JS 系 linter / tester を実行しない)
prettier = true

extend-exclude = [
    "target",
    "node_modules",
    "dist",
]
```

プロジェクト固有の許可語がある場合は`[tool.typos]`セクションも追記する
（詳細は[推奨設定例](recommended.md)の「typosの許可語設定」を参照）。

`mise.toml`例（cargo系をmise経由で固定バージョン・固定コンポーネントで起動）:

```toml
[tools]
# rust backend経由でcargo fmt / cargo clippyを確実に解決するためcomponentsを明示する
rust = { version = "1.83.0", components = "rustfmt,clippy" }
# cargo-denyはmise core registry非収録のためaquaレジストリ経由で取得する
"aqua:EmbarkStudios/cargo-deny" = "latest"
```

mise設定に`rust`の記述がある場合、バージョン固定・components指定がそのまま反映されるため
`cargo-fmt-version`等をpyfltr側で別途明示する二重管理は不要。
PATH上のcargoを使いたい場合は`cargo-fmt-runner = "direct"`等を指定する。

`.pre-commit-config.yaml`（ローカルフックで`uvx pyfltr fast`を呼ぶ）:

```yaml
  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uvx pyfltr fast
        types_or: [rust, markdown, toml, yaml, javascript, ts]
        require_serial: true
        language: system
```

ポイント:

- `pyfltr fast`: fix段を内蔵するため、
  linterのautofix（`cargo-clippy --fix`、`markdownlint --fix`等）→ formatter → 軽量linterの順で実行される
- `pyfltr run`: formatter差分を自動修正し、linter/tester通過で成功する（ローカル開発向け）
- `pyfltr ci`: formatter差分も含めて失敗扱いにする（CI向け）
- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ
    - `textlint-packages`は無視される
- タスクランナーの設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照

### Pythonルート＋非Pythonサブディレクトリのハイブリッド構成

Pythonプロジェクトルート直下に`rust/<crate>/Cargo.toml`のようなRust crateを
サブディレクトリとして持つ構成にも対応する。
サブプロジェクト検出はマーカー（`pyproject.toml`・`Cargo.toml`・`*.csproj`・`*.sln`）の
存在で判定する。
そのため`Cargo.toml`単独ディレクトリもサブプロジェクトとして認識し、
`cargo-clippy`・`cargo-check`・`cargo-test`・`cargo-deny`は当該ディレクトリを
cwdとして起動する。
同じ仕組みはPythonルート＋`.NET`のプロジェクトファイル
（`*.csproj`・`*.sln`）単独ディレクトリにも適用される。

ルート`pyproject.toml`（`rust/<crate>/`側への`pyproject.toml`追加は不要）:

```toml
[tool.pyfltr]
preset = "latest"
rust = true
```

`Cargo.toml`単独ディレクトリは`[tool.pyfltr]`の記述先を持たないため、
cargo系コマンドのON/OFF・除外設定はルート`pyproject.toml`の値をそのまま継承する。
`.pre-commit-config.yaml`の`types_or`にも`rust`を含める。

```yaml
  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uvx pyfltr fast
        types_or: [rust, python, markdown, toml, yaml]
        require_serial: true
        language: system
```

`package.json`は汎用ファイルのため単独ではサブプロジェクトとして検出しない。
JS専用サブディレクトリを独立サブプロジェクトとして分離したい場合は、
当該ディレクトリへ`pyproject.toml`（`[tool.pyfltr] javascript = true`等）を
追加配置する。

## .NETプロジェクト

`dotnet format` / `dotnet build` / `dotnet test`と、ドキュメント系lintをpyfltrに一元化する例。
Rustプロジェクト節の構成を基準に、cargo系コマンドをdotnet系に置き換える。

`mise.toml`例（dotnet SDKをmise経由で固定バージョンで起動）:

```toml
[tools]
dotnet = "9.0.100"
```

mise設定に`dotnet`の記述がある場合、バージョン固定がそのまま反映されるため
`dotnet-format-version`等をpyfltr側で別途明示する二重管理は不要。
direct実行へ戻したい場合は`dotnet-format-runner = "direct"`等を指定する。
direct実行時、pyfltrは環境変数`DOTNET_ROOT`配下の`dotnet`実行ファイルを優先する。

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
preset = "latest"
dotnet = true
js-runner = "pnpm"
# prettier はドキュメント系を pnpm で実行するために個別に opt-in する
# (javascript ゲートは開けず、.NET 専用プロジェクトで不要な JS 系 linter / tester を実行しない)
prettier = true

extend-exclude = [
    "bin",
    "obj",
    "publish",
    "node_modules",
]
```

`.pre-commit-config.yaml`:

```yaml
  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uvx pyfltr fast
        types_or: [c#, csproj, sln, msbuild, editorconfig, markdown, toml, yaml]
        require_serial: true
        language: system
```

ポイント:

- `types_or`にC#関連のタグ（`c#`、`csproj`、`sln`、`msbuild`、`editorconfig`）を含める
    - dotnet系コマンドは`pass-filenames = false`でプロジェクト全体をチェックするが、
    pre-commitはtypes_orに一致するファイルがコミットに含まれないとhook自体を起動しない
    - identifyライブラリのタグ名は`csharp`ではなく`c#`であることに注意
- `dotnet-build`: ビルドエラーをlint段階で検出するためlinterとして分類している
- `dotnet-format`: formatterとして常時書き込みモードで動作する
- タスクランナーの設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照
