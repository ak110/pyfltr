# 変更履歴

本ドキュメントはpyfltrの主な変更点を記録する。
形式は[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)に沿い、バージョン番号は[Semantic Versioning](https://semver.org/lang/ja/)に従う。
v3.0.0から整備を開始した。それ以前の変更はgit logおよびGitHubリリースを参照。

## [Unreleased] (3.0.0 予定)

破壊的変更を多く含む。
移行手順は[docs/v3/マイグレーションガイド.md](docs/v3/マイグレーションガイド.md)を参照。

### 破壊的変更

- サブコマンドを必須化。
  引数なし実行時の`ci`フォールバックを廃止し、すべての呼び出しで`pyfltr ci` / `pyfltr run`等を明示する必要がある
- 5ツール（`pyupgrade` / `autoflake` / `isort` / `black` / `pflake8`）を削除。
  ruffへの統合で代替可能となったため、関連設定キー（`pyupgrade-args`等）も受け付けなくなった
- プリセット`"20250710"`を削除。
  削除5ツール向けの設定しか持たなかったため廃止。`preset = "latest"`または`"20260413"`等へ移行する
- Python系ツール（`ruff-format` / `ruff-check` / `mypy` / `pylint` / `pyright` / `ty` / `pytest` / `uv-sort`）をopt-in化。
  既定で無効となり、`python = true`または`{command} = true`で明示的に有効化する
- Python系ツールの依存を`pyfltr[python]`オプショナルグループへ分離。
  利用時は`pip install 'pyfltr[python]'`または`uv add 'pyfltr[python]'`で導入する
- 本体必須依存を`mcp` / `natsort` / `platformdirs` / `python-ulid` / `pyyaml` / `textual`に限定

### 追加

- 実行アーカイブ機能。
  ツール生出力・diagnostic・メタ情報を実行ごとに`platformdirs.user_cache_dir("pyfltr")`配下へ保存する
  - 既定で有効。`--no-archive`や`archive = false`でオプトアウト可能
  - 自動クリーンアップ: 世代数100 / 合計1GB / 30日のいずれかを超過すると古い順に削除
  - 関連設定キー: `archive` / `archive-max-runs` / `archive-max-size-mb` / `archive-max-age-days`
- JSONL出力の拡張
  - `header.run_id`（ULID）: 実行アーカイブの参照キー
  - `diagnostic.rule_url`: 対応ツール（ruff / pylint / pyright / mypy / shellcheck / eslint / markdownlint）のルールドキュメントURL
  - `diagnostic.severity`: `error` / `warning` / `info`の3値に正規化
  - `tool.retry_command`: 1ツール再実行用のshellコマンド文字列
  - `tool.truncated`: smart truncation発生時の切り詰め前情報とアーカイブパス
- smart truncation。
  JSONL出力側でdiagnostic件数および`tool.message`の行数・文字数を制限し、アーカイブには全文を保存する
  - 関連設定キー: `jsonl-diagnostic-limit` / `jsonl-message-max-lines` / `jsonl-message-max-chars`
- 出力形式: `--output-format=sarif`（SARIF 2.1.0互換）と`--output-format=github-annotations`（GitHub Actions向け注釈）

### 変更

- `run-for-agent`サブコマンドの既定出力形式を`jsonl`とする（`pyfltr run --output-format=jsonl`と等価）
