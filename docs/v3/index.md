# v3.0.0 概要

pyfltr v3.0.0で実施する破壊的変更とロードマップ機能群の全体像。
本ドキュメントはエージェント連携・多言語プロジェクト対応・再実行回避を軸とした再出発のガイド。

## 方針

- 破壊的変更を含むメジャーリリースとする
- エージェント連携（JSONL・実行アーカイブ・MCP）を軸にユーザー体験を刷新する
- 非Pythonプロジェクトでの利用障壁を取り除く
- 削除・刷新により内部実装を理想的な形に戻す

## パート構成

v3.0.0は6パートに分割して段階実装する。

| パート | 主題 | 概要 |
| --- | --- | --- |
| A | 依存整理・破壊的変更 | Python 系オプトイン化、5 ツール完全削除、プリセット 20250710 削除、依存の extras 化 |
| B | 実行アーカイブ基盤 | 全実行の詳細をユーザーキャッシュへ自動保存・run_id 発行・自動クリーンアップ |
| C | JSONL 出力拡張 | severity 正規化完了・rule_url 付与・リトライ提案埋め込み・smart truncation・SARIF / GitHub Annotation 出力 |
| D | パイプライン機能拡張 | `--fail-fast`、ファイル hash ベースのスキップキャッシュ |
| E | 詳細参照サブコマンド | `show-run` / `list-runs` サブコマンド追加 |
| F | MCP サーバー化 | `pyfltr mcp` サブコマンドでの MCP サーバー本体同梱、詳細参照 API の提供、stdio 隔離 |

各パートの設計判断・実装詳細は個別ドキュメントに分離する。

- [依存整理と破壊的変更](依存整理と破壊的変更.md)
- [実行アーカイブ](実行アーカイブ.md)
- [JSONL 出力拡張](JSONL出力拡張.md)
- [マイグレーションガイド](マイグレーションガイド.md)
- [作業ステータス](作業ステータス.md)

## 主な破壊的変更

既存ユーザーが影響を受ける主要な変更は次の通り。

- サブコマンドが必須化された（省略時の`ci`フォールバック廃止）
- Python系ツールがopt-in化された（既定値False）
- プリセット `"20250710"` が削除された
- ツール5種が削除された (`pyupgrade` / `autoflake` / `isort` / `black` / `pflake8`)
- Python系linter / testerは `pip install pyfltr[python]` で別途導入する

具体的な移行手順は [マイグレーションガイド](マイグレーションガイド.md) を参照。

## 主な追加機能

- 実行アーカイブ: 全実行の詳細をユーザーキャッシュへ自動保存する ([実行アーカイブ](実行アーカイブ.md))
- `--no-archive`: 実行アーカイブを一時的に無効化するCLIオプション
- JSONLの `header` レコードに `run_id` フィールドが追加された（アーカイブ参照キー）
- JSONL出力拡張 ([JSONL 出力拡張](JSONL出力拡張.md)): severity正規化・`rule_url` / `retry_command` / smart truncation・SARIF 2.1.0 / GitHub Annotation形式への出力対応

## 依存関係の整理

本体必須依存は次の4カテゴリのみに限定する。

- 骨組み（TUI・並列実行・出力整形・ファイル展開・設定読込）: `textual` / `natsort` / `pyyaml`
- 全言語共通ツール連携: `textlint` / `markdownlint` / `typos` / `actionlint`（本体依存ではなく実行時にツール側の存在を要求）
- run_id生成: `python-ulid`
- MCPサーバー同梱: `mcp` / `platformdirs`

Python系linter / tester (`mypy` / `pylint` / `pyright` / `ty` / `pytest` / `ruff` / `uv-sort` など) は `pyfltr[python]` オプショナルグループに分離する。
