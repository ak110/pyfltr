# 利用者向けガイド

## 対応ツール

対応ツールを言語・用途別に示す。
言語カテゴリ（Python / JS/TS / Rust / .NET）に属するツールはすべて既定で無効（opt-in）。
有効化には、該当する言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）を`true`にするか、個別に`{command} = true`と明示する。
`preset = "latest"`は言語非依存のツールとドキュメント系のみ有効化する。
詳細なプリセット挙動と言語カテゴリの一括有効化は[設定項目](configuration.md)を参照。

### Python系

いずれも既定で無効（opt-in）。`pyproject.toml`に`python = true`を設定するか、個別に`{command} = true`を指定する。
同時に`pip install pyfltr[python]`でPython系ツールの依存を追加する必要がある。

- Formatters: ruff format / uv-sort（依存定義のソート）
- Linters: ruff check / mypy / pylint / pyright / ty
- Testers: pytest

### JS/TS系

いずれも既定で無効（opt-in）。`javascript = true`で一括有効化できる（TypeScriptも同カテゴリ）。
`js-runner`設定で起動方式（pnpx / pnpm / npx等）を切り替える。

- Formatters: prettier
- Linters: eslint / biome / oxlint / tsc（型チェック。`pass-filenames = false`でプロジェクト全体を対象）
- Testers: vitest

### Rust系

いずれも既定で無効（opt-in）。`rust = true`で一括有効化できる。
プロジェクト全体（crate単位）を対象に直接実行する（`{command}-path`で実行パスを指定）。

- Formatters: cargo fmt
- Linters: cargo clippy / cargo check / cargo deny（依存ライセンス・脆弱性チェック）
- Testers: cargo test

### .NET系

いずれも既定で無効（opt-in）。`dotnet = true`で一括有効化できる。
プロジェクト全体（solution単位）を対象に直接実行する（`{command}-path`で実行パスを指定）。

- Formatters: dotnet format
- Linters: dotnet build（ビルドエラーをlint段階で検出）
- Testers: dotnet test

### ドキュメント系

- Linters: markdownlint-cli2 / textlint

### その他

- Formatters: shfmt（既定で無効）
- Linters: ec（editorconfig-checker、既定で無効）/ shellcheck（既定で無効）/ typos / actionlint
- 統合: pre-commit（`.pre-commit-config.yaml`のhookを統合実行）

プリセット・言語カテゴリキーで一括有効化する方法は[設定項目](configuration.md)を参照。

個別に有効化・無効化する方法や`bin-runner`/`js-runner`などの補助設定は[設定項目（ツール別）](configuration-tools.md)を参照。

## コンセプト

- 各種ツールをまとめて並列で呼び出し、実行時間を短縮する
- 各種ツールのバージョンには極力依存しない（各ツール固有の設定には対応しない）
- excludeの指定方法が各ツールで異なる問題を、pyfltr側で解決してツールに渡すことで吸収する
- formatterはファイルを修正しつつエラーとしても扱う（`pyfltr ci`ではformatterによる変更も失敗と判定する）
- 設定は極力`pyproject.toml`に集約する

## インストール

```shell
pip install pyfltr
```

## ガイドページ

- [CLIコマンド](usage.md) — CLIの使い方・サブコマンド・オプション
- [設定項目](configuration.md) — 基本設定・プリセット・並列実行
- [設定項目（ツール別）](configuration-tools.md) — ツール別設定（直接実行 / js-runner / bin-runnerのカテゴリ別設定・2段階実行・カスタムコマンド）
- [推奨設定例](recommended.md) — 推奨設定（Pythonプロジェクト・タスクランナー・CI）
- [推奨設定例（非Pythonプロジェクト）](recommended-nonpython.md) — 非Pythonプロジェクトの推奨設定
- [カスタムコマンド例](custom-commands.md) — カスタムコマンドの設定例
- [v3.0.0マイグレーションガイド](migration-v3.md) — v2.xからv3.0.0への移行手順
