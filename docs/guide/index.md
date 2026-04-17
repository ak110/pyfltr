# 利用者向けガイド

## 対応ツール

対応ツールを言語・用途別に示す。
末尾に「既定で無効」と記した項目は、`preset = "latest"`でも有効化されないため`pyproject.toml`で`{command} = true`と明示する必要がある。
それ以外の項目は既定または`preset = "latest"`で有効化される。詳細なプリセット挙動は[設定項目](configuration.md)を参照。

### Python系

いずれも既定で無効（opt-in）。利用時は`pyproject.toml`に`python = true`を設定するか、個別に`{command} = true`を指定する。
同時に`pip install pyfltr[python]`でPython系ツールの依存を追加する必要がある。

- Formatters: ruff format / uv-sort（依存定義のソート）
- Linters: ruff check / mypy / pylint / pyright / ty
- Testers: pytest

### JS/TS系

いずれも既定で無効。`js-runner`設定で起動方式（pnpx / pnpm / npx等）を切り替える。

- Formatters: prettier
- Linters: eslint / biome / oxlint / tsc（型チェック。`pass-filenames = false`でプロジェクト全体を対象）
- Testers: vitest

### Rust系

いずれも既定で無効。プロジェクト全体（crate単位）を対象に直接実行する（`{command}-path`で実行パスを指定）。

- Formatters: cargo fmt
- Linters: cargo clippy / cargo check / cargo deny（依存ライセンス・脆弱性チェック）
- Testers: cargo test

### .NET系

いずれも既定で無効。プロジェクト全体（solution単位）を対象に直接実行する（`{command}-path`で実行パスを指定）。

- Formatters: dotnet format
- Linters: dotnet build（ビルドエラーをlint段階で検出）
- Testers: dotnet test

### ドキュメント系

- Linters: markdownlint-cli2 / textlint

### その他

- Formatters: shfmt（既定で無効）
- Linters: ec（editorconfig-checker、既定で無効）/ shellcheck（既定で無効）/ typos / actionlint
- 統合: pre-commit（`.pre-commit-config.yaml`のhookを統合実行）

プリセットで一括有効化する方法は[設定項目](configuration.md)を参照。

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
