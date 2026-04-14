# 利用者向けガイド

## 対応ツール

対応ツールの一覧を分類別に示す。
既定で有効化されるツールは`preset`の値によって決まる。
プリセットごとの有効ツールは[設定項目](configuration.md)の「プリセット設定」で確認できる。
個別に有効化・無効化する方法や`bin-runner`/`js-runner`などの補助設定は[設定項目（ツール別）](configuration-tools.md)を参照。

- Formatters
    - pyupgrade
    - autoflake
    - isort
    - black
    - ruff format
    - prettier
    - uv-sort（依存定義のソート）
    - shfmt
    - cargo fmt（crate全体を対象）
    - dotnet format（solution全体を対象）
- Linters
    - ruff check
    - pflake8 + flake8-bugbear + flake8-tidy-imports
    - mypy
    - pylint
    - pyright
    - ty
    - ec（editorconfig-checker）
    - shellcheck
    - typos
    - actionlint
    - markdownlint-cli2
    - textlint
    - eslint
    - biome
    - oxlint
    - tsc（型チェックのみ実行）
    - cargo clippy
    - cargo check
    - cargo deny（依存ライセンス・脆弱性チェック）
    - dotnet build（ビルドエラーをlint段階で検出）
- Testers
    - pytest
    - vitest
    - cargo test
    - dotnet test

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

- [CLIコマンド](usage.md) — CLI使い方・サブコマンド・オプション
- [設定項目](configuration.md) — 基本設定・プリセット・並列実行
- [設定項目（ツール別）](configuration-tools.md) — ツール別設定（2段階実行・bin-runner・npm系・カスタムコマンド）
- [推奨設定例](recommended.md) — 推奨設定（Pythonプロジェクト・タスクランナー・CI）
- [推奨設定例（非Pythonプロジェクト）](recommended-nonpython.md) — 非Pythonプロジェクトの推奨設定
- [カスタムコマンド例](custom-commands.md) — カスタムコマンドの設定例
