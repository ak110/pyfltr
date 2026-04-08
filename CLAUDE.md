# カスタム指示 (プロジェクト固有)

- `pyproject.toml` の編集は極力 `uv` コマンドを使う (`uv add`, `uv remove` など)
  - 手動編集は `uv` コマンドでは対応できない箇所に限る
- コマンドラインを記述するときは可読性のため極力 `--foo` のようなロング形式のオプションを使用する

## 開発手順

- `make format`: 整形 + 軽量lint + 自動修正（開発時の手動実行用）
- `make test`: 全チェック実行（これが成功すればコミットしてよい）
- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- `make docs`: ドキュメントのローカルプレビュー
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- テストコードの実行は `uv run pyfltr <path>` を使う (pytestを直接呼び出さない)
  - `-vv`などが必要な場合に限り `uv run pyfltr -vv <path>` のようにする
- Markdownファイルのformat/lintの実行方法: `uv run pre-commit run --files <file>`

## 外部ツール仕様の確認

- ruff / mypy / pytest / pylint / pyright / ty など対応ツールの最新仕様を参照する際は、
  `context7` MCP (`mcp__plugin_context7_context7__resolve-library-id` →
  `mcp__plugin_context7_context7__query-docs`) を優先する
- pyfltr は対応ツールのバージョン追従が宿命のため、知識のスナップショットではなく
  最新ドキュメントを確認する

## 関連ドキュメント

- @README.md
- @docs/index.md
- @docs/development.md
- ドキュメント追加時は `mkdocs.yml` の `nav` を更新要
