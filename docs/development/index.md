# 開発者向けガイド

pyfltr本体の保守・貢献に必要な情報をまとめている。
対象読者は本リポジトリへパッチを送る開発者や、ローカルでpyfltr自身の動作確認を行う利用者。

## 開発手順

- [開発手順](development.md) — 開発環境構築・サプライチェーン対策・ドキュメント運用・リリース手順

## 内部設計

- [アーキテクチャ概要](architecture.md) — 実行パイプライン・モジュール構成・主要設計判断
- [実行アーカイブとファイルhashキャッシュ](archive-and-cache.md) — `<cache_root>/runs/`と`<cache_root>/cache/`の保存先・自動クリーンアップ・対象判定
- [JSONL出力の設計](jsonl-output.md) — severity正規化・rule_url・retry_command・smart truncation・SARIF/GitHub Annotation
- [詳細参照サブコマンドと再実行支援](subcommands.md) — `list-runs`/`show-run`の実装配置・`--only-failed`/`--from-run`の絞り込み戦略
- [MCPサーバー](mcp-server.md) — `pyfltr mcp`サブコマンドとMCPツール5種・FastMCP採用・stdio隔離
