# 推奨設定例（非Pythonプロジェクト）

Python以外のプロジェクトでもpyfltrを活用できる。共通のポイントは以下。

- `preset = "latest"`: 各時点での推奨ツール構成。ドキュメント系（markdownlint / textlint / actionlint / typos / pre-commit）はいずれの言語ゲートにも属さず常に有効化される
- 言語別ツールはv3.0.0以降すべてopt-in。利用する言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）を`true`にしてゲートを開ける。現行プリセットには言語別ツールが含まれないため、個別に`{command} = true`で有効化するのが基本
- `uvx pyfltr`: pyfltrをdev依存に含めないため、`uvx`で都度取得して実行する
- 言語固有のツール + ドキュメント系lint（textlint / markdownlint / prettier）を組み合わせる
- `bin-runner`のデフォルトは`"mise"`。actionlint / typos等のネイティブバイナリツールはmise経由で呼び出されるため、mise導入とツールのセットアップ（`mise use actionlint@latest typos@latest`等）を推奨する
- タスクランナー（Makefile / mise.toml）の設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照

## TypeScript/JS専用プロジェクト

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
preset = "latest"
javascript = true
# 現行プリセットにはJS/TS系が含まれないため、利用するツールを個別に有効化する
eslint = true
prettier = true
biome = true
tsc = true
vitest = true
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

- `javascript = true`: JS/TS系ツールのゲートを開ける。現行プリセットにJS/TS系は含まれないため、利用するツールを個別に`{command} = true`で有効化する
- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ。`textlint-packages`は無視される
- eslintとoxlintは併用するとeslintで非対応のルールを補完できる（Rust製のため高速）
- tsc: TypeScript型チェックも実行できる。svelte-checkなどフレームワーク固有のチェッカーと併用する場合はどちらか一方でよい
- vitest: `vitest-args = ["run"]`が既定のため追加引数は不要
- svelte-checkなどフレームワーク固有のツールはカスタムコマンドで追加する（[カスタムコマンド例](custom-commands.md)の「svelte-check」を参照）

## Rustプロジェクト

`cargo fmt` / `cargo clippy` / `cargo test` / `cargo deny`と、ドキュメント系lint（`textlint` / `markdownlint-cli2` / `prettier`）をpyfltrに一元化する例。
v3.0.0以降、言語カテゴリはすべてopt-inのため、非Rustプロジェクトでcargo系が走ることはない。

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
preset = "latest"
rust = true
# 現行プリセットにはRust系が含まれないため、利用するcargoコマンドを個別に有効化する
cargo-fmt = true
cargo-clippy = true
cargo-check = true
cargo-test = true
cargo-deny = true
js-runner = "pnpm"
# prettier はドキュメント系を pnpm で回すために個別に opt-in する
prettier = true

extend-exclude = [
    "target",
    "node_modules",
    "dist",
]
```

プロジェクト固有の許可語がある場合は`[tool.typos]`セクションも追記する（詳細は[推奨設定例](recommended.md)の「typosの許可語設定」を参照）。

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

- `format`: `pyfltr fast`がfix段を内蔵するため、linterのautofix（`cargo-clippy --fix`、`markdownlint --fix`等）→ formatter → 軽量linterの順で実行される。
- `test`: ローカル開発用。`pyfltr run`はformatter差分を自動修正し、linter/tester通過で成功する。
- `ci`: CI用。`pyfltr ci`はformatter差分も含めて失敗扱いにする。
- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ。`textlint-packages`は無視される。
- タスクランナーの設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照。

## .NETプロジェクト

`dotnet format` / `dotnet build` / `dotnet test`と、ドキュメント系lintをpyfltrに一元化する例。
Rustプロジェクトと同じ構成で、cargo系をdotnet系に差し替える。

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
preset = "latest"
dotnet = true
# 現行プリセットには.NET系が含まれないため、利用するdotnetコマンドを個別に有効化する
dotnet-format = true
dotnet-build = true
dotnet-test = true
js-runner = "pnpm"
# prettier はドキュメント系を pnpm で回すために個別に opt-in する
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

- `types_or`にC#関連のタグ（`c#`、`csproj`、`sln`、`msbuild`、`editorconfig`）を含める。dotnet系コマンドは`pass-filenames = false`でプロジェクト全体をチェックするが、pre-commitはtypes_orに一致するファイルがコミットに含まれないとhook自体を起動しない。identifyライブラリのタグ名は`csharp`ではなく`c#`であることに注意。
- `dotnet-build`: ビルドエラーをlint段階で検出するためlinterとして分類している。
- `dotnet-format`: formatterとして常時書き込みモードで動作する。
- タスクランナーの設定例は[推奨設定例](recommended.md)の「タスクランナー」を参照。
