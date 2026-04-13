# 非Pythonプロジェクト推奨設定例

Python以外のプロジェクトでもpyfltrを活用できる。共通のポイントは以下。

- `python = false`: Python系ツールを一括無効化する（対象ツールの一覧は[docs/guide/configuration.md](configuration.md)の「Python系ツールの一括無効化」を参照）
- `uvx pyfltr`: pyfltrをdev依存に含めないため、`uvx`で都度取得して実行する
- 言語固有のツール + ドキュメント系lint（textlint / markdownlint / prettier）を組み合わせる
- タスクランナー（Makefile / mise.toml）の設定例は[docs/guide/recommended.md](recommended.md)の「タスクランナー」を参照

## TypeScript/JS専用プロジェクト

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
python = false
pre-commit = true
js-runner = "pnpm"
eslint = true
prettier = true
markdownlint = true
textlint = true
vitest = true
oxlint = true

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

- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ。`textlint-packages`は無視される。
- `oxlint = true`: eslintと併用するとeslintで非対応のルールを補完できる。Rust製のため高速。
- `tsc = true`を追加するとTypeScript型チェックも実行できる。svelte-checkなどフレームワーク固有のチェッカーと併用する場合はどちらか一方でよい。
- vitest: `vitest-args = ["run"]`が既定のため追加引数は不要。
- svelte-checkなどフレームワーク固有のツールはカスタムコマンドで追加する（[docs/guide/custom-commands.md](custom-commands.md)の「svelte-check」を参照）。

## Rustプロジェクト

`cargo fmt` / `cargo clippy` / `cargo test` / `cargo deny`と、ドキュメント系lint（`textlint` / `markdownlint-cli2` / `prettier`）をpyfltrに一元化する例。
Python非依存プロジェクトのため`python = false`でPython系ツールを一括無効化する。

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
python = false
pre-commit = true
js-runner = "pnpm"
bin-runner = "mise"
cargo-fmt = true
cargo-clippy = true
cargo-check = true
cargo-test = true
cargo-deny = true
markdownlint = true
textlint = true
prettier = true

extend-exclude = [
    "target",
    "node_modules",
    "dist",
]
```

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

- `format`: `pyfltr fix`でlinterのautofix（`cargo-clippy --fix`、`markdownlint --fix`等）を実行した後、`pyfltr fast`でformatter + 軽量linterを実行する。
- `test`: ローカル開発用。`pyfltr run`はformatter差分を自動修正し、linter/tester通過で成功する。
- `ci`: CI用。`pyfltr ci`はformatter差分も含めて失敗扱いにする。
- `js-runner = "pnpm"`: pnpmワークスペース経由でJS系ツールを呼ぶ。`textlint-packages`は無視される。
- `bin-runner = "mise"`: miseのshim経由でネイティブバイナリツール（shellcheck等）を呼ぶ。cargoは直接呼び出すため影響しない。
- タスクランナーの設定例は[docs/guide/recommended.md](recommended.md)の「タスクランナー」を参照。

## .NETプロジェクト

`dotnet format` / `dotnet build` / `dotnet test`と、ドキュメント系lintをpyfltrに一元化する例。
Rustプロジェクトと同じ構成で、cargo系をdotnet系に差し替える。

`pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "1 day"

[tool.pyfltr]
python = false
pre-commit = true
js-runner = "pnpm"
bin-runner = "mise"
dotnet-format = true
dotnet-build = true
dotnet-test = true
markdownlint = true
textlint = true
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
- タスクランナーの設定例は[docs/guide/recommended.md](recommended.md)の「タスクランナー」を参照。
