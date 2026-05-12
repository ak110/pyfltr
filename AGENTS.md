# CLAUDE.md: pyfltr

Python/Rust/.NET/TypeScript・JavaScript/ドキュメントなど多言語プロジェクトの
formatter・linter・testerを単一コマンドで並列実行するCLIツール。
JSON Lines出力（`--output-format=jsonl`）とMCPサーバー（`pyfltr mcp`）でコーディングエージェント運用にも対応する。

## 開発手順

- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- リリース手順: `gh workflow run release.yaml --field=bump=PATCH`（`PATCH`は`MINOR`・`MAJOR`に変更可）
- Docker再ビルド単発起動: `gh workflow run docker-build.yaml`
  - `ghcr.io/ak110/pyfltr:latest`をリリースを伴わず更新する
  - `--field=version=X.Y.Z`で特定バージョンを指定する。未指定時はPyPI最新公開版を採用する
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- コミット前の検証方法: `uv run --with-editable=. pyfltr run-for-agent`
  - `--with-editable=.`はローカル編集中のpyfltrを利用するための指定
  - テストコードの単体実行なども極力`pyfltr run-for-agent <path>`を使う（直接呼び出さない）
  - 修正後の再実行時は`--commands=mypy,ruff-check`等で限定して実行する（最終検証はCIに委ねる前提）

## アーキテクチャの参照先

サブパッケージ・モジュールごとの構成詳細とサブパッケージ間の依存方向、
format別のlogger stream/level切替の詳細は[docs/development/architecture.md](docs/development/architecture.md)を参照する。
