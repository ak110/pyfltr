# カスタム指示 (プロジェクト固有)

@CLAUDE.base.md

- `pyproject.toml` の編集は極力 `uv` コマンドを使う (`uv add`, `uv remove` など)
  - 手動編集は `uv` コマンドでは対応できない箇所に限る

## 関連ドキュメント

- @README.md
- @docs/index.md
- @docs/development.md
- @docs/style-guide.md
- ドキュメント追加時は `mkdocs.yml` の `nav` を更新要
