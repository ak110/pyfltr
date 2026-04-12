# 利用者向けガイド

## 対応ツール

- Formatters
    - pyupgrade
    - autoflake
    - isort
    - black
    - ruff format（既定では無効。有効時は `ruff check --fix --unsafe-fixes` を併走する、`ruff-format-by-check`でOFF可）
    - prettier（既定では無効、`js-runner`設定で起動方式を切替可能。`--check`と`--write`の2段階実行）
    - uv-sort（既定では無効、`pyproject.toml`の依存ソート）
    - shfmt（既定では無効、`bin-runner`設定で起動方式を切替可能。prettierと同様の2段階実行）
    - cargo fmt（既定では無効、`pass-filenames = false`でcrate全体を対象）
    - dotnet format（既定では無効、`pass-filenames = false`でsolution全体を対象）
- Linters
    - ruff check（既定では無効）
    - pflake8 + flake8-bugbear + flake8-tidy-imports
    - mypy
    - pylint
    - pyright（既定では無効）
    - ty（既定では無効）
    - ec（editorconfig-checker。既定では無効、`bin-runner`設定で起動方式を切替可能）
    - shellcheck（既定では無効、`bin-runner`設定で起動方式を切替可能）
    - typos（既定では無効、`bin-runner`設定で起動方式を切替可能）
    - actionlint（既定では無効、`bin-runner`設定で起動方式を切替可能）
    - markdownlint-cli2（既定では無効、`js-runner`設定で起動方式を切替可能。既定は`pnpx`）
    - textlint（既定では無効、`js-runner`設定で起動方式を切替可能。`textlint-packages`でプリセット/ルール指定）
    - eslint（既定では無効、`js-runner`設定で起動方式を切替可能。`--format json`で機械可読出力を取得）
    - biome（既定では無効、`js-runner`設定で起動方式を切替可能。`biome check`サブコマンドと`--reporter=github`を使用）
    - oxlint（既定では無効、`js-runner`設定で起動方式を切替可能）
    - tsc（既定では無効、`js-runner`設定で起動方式を切替可能。`--noEmit`で型チェックのみ実行）
    - cargo clippy（既定では無効、fixモード対応）
    - cargo check（既定では無効）
    - cargo deny（既定では無効、依存ライセンス・脆弱性チェック）
    - dotnet build（既定では無効、ビルドエラーをlint段階で検出）
- Testers
    - pytest
    - vitest（既定では無効、`js-runner`設定で起動方式を切替可能。`run`サブコマンドで実行）
    - cargo test（既定では無効）
    - dotnet test（既定では無効）

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

- [usage.md](usage.md) — CLI使い方・サブコマンド・オプション
- [configuration.md](configuration.md) — 基本設定・プリセット・並列実行
- [configuration-tools.md](configuration-tools.md) — ツール別設定（2段階実行・bin-runner・npm系・カスタムコマンド）
- [recommended.md](recommended.md) — 推奨設定（Pythonプロジェクト・タスクランナー・CI）
- [recommended-nonpython.md](recommended-nonpython.md) — 非Pythonプロジェクトの推奨設定
- [custom-commands.md](custom-commands.md) — カスタムコマンドの設定例
