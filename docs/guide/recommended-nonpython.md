# 推奨設定例（非Pythonプロジェクト）

Python以外のプロジェクトでもpyfltrを活用できる。共通のポイントは以下。

- `preset = "latest"`: 各時点での推奨ツール構成。
  ドキュメント系（textlint / markdownlint / actionlint / typos / pre-commit）は言語カテゴリゲートに属さず、
  プリセットでtrueになっているツールがそのまま有効化される
- 言語カテゴリゲートの詳細は[設定項目](configuration.md)を参照
- `uvx pyfltr`: pyfltrをdev依存に含めないため、`uvx`で都度取得して実行する
- 言語固有のツール + ドキュメント系lint（textlint / markdownlint / prettier）を組み合わせる
- `bin-runner`の既定は`"mise"`。actionlint等のネイティブバイナリツールはmise経由で呼び出されるため、mise導入を推奨する
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
- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ。`textlint-packages`は無視される
- eslintとoxlintは併用するとeslintで非対応のルールを補完できる（Rust製のため高速）
- tsc: TypeScript型チェックも実行できる。svelte-checkなどフレームワーク固有のチェッカーと併用する場合はどちらか一方でよい
- vitest: `vitest-args = ["run", "--passWithNoTests"]`が既定のため追加引数は不要
- 使わないツールは個別に`{command} = false`で無効化できる
- svelte-checkなどフレームワーク固有のツールはカスタムコマンドで追加する
 （[カスタムコマンド例](custom-commands.md)の「svelte-check」を参照）

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
# (javascript ゲートは開けず、Rust 専用プロジェクトで不要な JS 系 linter / tester を走らせない)
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

`pyfltr`は`bin-runner`既定`"mise"`に従い、mise設定（プロジェクトまたはグローバル）に`rust`記述がある場合は
`mise exec -- cargo`としてtool specを省略する。
mise設定の解決済み内容（バージョン固定・componentsなど）がそのまま反映されるため、
`cargo-fmt-version`等をpyfltr側で別途明示する二重管理は不要。
mise設定に`rust`記述が無い場合は従来通り`mise exec rust@latest -- cargo`を組み立てる。
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

- `format`: `pyfltr fast`がfix段を内蔵するため、
  linterのautofix（`cargo-clippy --fix`、`markdownlint --fix`等）→ formatter → 軽量linterの順で実行される。
- `test`: ローカル開発用。`pyfltr run`はformatter差分を自動修正し、linter/tester通過で成功する。
- `ci`: CI用。`pyfltr ci`はformatter差分も含めて失敗扱いにする。
- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ。`textlint-packages`は無視される。
- タスクランナーの設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照。

## .NETプロジェクト

`dotnet format` / `dotnet build` / `dotnet test`と、ドキュメント系lintをpyfltrに一元化する例。
Rustプロジェクトと同じ構成で、cargo系をdotnet系に差し替える。

`mise.toml`例（dotnet SDKをmise経由で固定バージョンで起動）:

```toml
[tools]
dotnet = "9.0.100"
```

`bin-runner`既定`"mise"`に従い、mise設定に`dotnet`記述がある場合は`mise exec -- dotnet`としてtool specを省略する。
mise設定の解決済み内容（固定バージョンなど）がそのまま反映されるため、
`dotnet-format-version`等をpyfltr側で別途明示する二重管理は不要。
mise設定に`dotnet`記述が無い場合は従来通り`mise exec dotnet@latest -- dotnet`相当を組み立てる。
direct実行へ戻したい場合は`dotnet-format-runner = "direct"`等を指定する。
direct実行時は環境変数`DOTNET_ROOT`配下の`dotnet`実行ファイルを優先採用する。

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
preset = "latest"
dotnet = true
js-runner = "pnpm"
# prettier はドキュメント系を pnpm で実行するために個別に opt-in する
# (javascript ゲートは開けず、.NET 専用プロジェクトで不要な JS 系 linter / tester を走らせない)
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

- `types_or`にC#関連のタグ（`c#`、`csproj`、`sln`、`msbuild`、`editorconfig`）を含める。
  dotnet系コマンドは`pass-filenames = false`でプロジェクト全体をチェックするが、
  pre-commitはtypes_orに一致するファイルがコミットに含まれないとhook自体を起動しない。
  identifyライブラリのタグ名は`csharp`ではなく`c#`であることに注意。
- `dotnet-build`: ビルドエラーをlint段階で検出するためlinterとして分類している。
- `dotnet-format`: formatterとして常時書き込みモードで動作する。
- タスクランナーの設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照。
