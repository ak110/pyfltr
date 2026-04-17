# CLAUDE.md: pyfltr

## 開発手順

- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- 実行パイプラインの構造: `run_pipeline`（main.py）がTUI/非TUI分岐の最上位関数。パイプライン共通の前処理（ファイル展開など）はこの関数内でTUI起動前に実行する
- コミット前の検証方法: `uv run pyfltr run-for-agent`
  - ドキュメントなどのみの変更の場合は省略可（pre-commitで実行されるため）
  - テストコードの単体実行なども極力 `uv run pyfltr run-for-agent <path>` を使う（pytestを直接呼び出さない）
    - 詳細な情報などが必要な場合に限り `uv run pytest -vv <path>` などを使用
  - JSONL出力は`header`（実行環境）→ `diagnostic`+`tool`（ツール完了ごと）→ `warning`→ `summary`（末尾）の順に出力される。末尾の`summary`で`failed`と`diagnostics`を確認し、必要に応じて`diagnostic`行のファイル・行番号・メッセージを参照する。詳細仕様は`docs/guide/usage.md`の「jsonlスキーマ」節および`llms.txt`を参照

## 注意点

- `uv run mkdocs build --strict`でリンク・nav整合性を検証（ただし日本語アンカーリンク`#見出し日本語`はMkDocs TOCで解決できずINFO通知のみで`--strict`でも検知されないため手動確認要）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は人手同期（SSOT化しない運用）
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する（`run-for-agent`サブコマンド、主要オプションなど）。LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- ドキュメント構成変更時は`docs/development/development.md`の「READMEとdocsの役割分担」節を先に参照
