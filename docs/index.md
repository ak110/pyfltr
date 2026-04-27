# pyfltr

Python / Rust / .NET / TypeScript・JavaScript / ドキュメントなど多言語プロジェクトの
formatter・linter・testerを単一コマンドで並列実行するCLIツール。

## 特徴

- 多言語対応のformatter・linter・testerを単一コマンドで並列実行
- 設定を`pyproject.toml`へ集約し、除外指定の書式差をツール間で吸収
- `pyfltr run-for-agent`でLLMエージェント向けJSON Lines出力に切り替え

## ドキュメント入口

- [利用者向けガイド](guide/index.md) — 対応ツール一覧・CLIの使い方・設定リファレンス・推奨構成
- [開発者向けガイド](development/development.md) — 開発環境構築・ドキュメント運用・リリース手順・ロードマップ

インストール手順と短いクイックスタートは[README](https://github.com/ak110/pyfltr)を参照。
