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
  - JSONL出力は`header`（実行環境・`run_id`）→ `diagnostic`+`tool`（ツール完了ごと）→ `warning`→ `summary`（末尾）の順に出力される。末尾の`summary`で`failed`と`diagnostics`を確認し、必要に応じて`diagnostic`行のファイル・行番号・メッセージを参照する。`header.run_id`（ULID）は実行アーカイブの参照キー
  - `diagnostic`の任意フィールド: `rule`・`rule_url`（対応ツールのみ）・`severity`（3値に正規化）・`fix`
  - `tool`の任意フィールド: `retry_command`（当該ツール1件の再実行コマンド）・`truncated`（smart truncation発生時。`archive`パスで全文参照）
  - `tool`のキャッシュ関連フィールド: `cached`（ファイルhashキャッシュから復元されたとき `true`）・`cached_from`（`cached=true` 時のソース `run_id`）
  - 詳細仕様は`docs/guide/usage.md`の「jsonlスキーマ」節および`llms.txt`を参照。`--output-format=sarif` / `github-annotations` でCI向け形式にも切り替え可能
  - `--fail-fast`: 1ツールでもエラーが出た時点で残りを打ち切る（起動済みはterminate、未開始はskipped扱い）
  - `--no-cache`: ファイルhashキャッシュを無効化する。現状はtextlintのみ対象
  - `header.run_id`はユーザーキャッシュに保存された該当runの参照キー。`pyfltr list-runs`で一覧、`pyfltr show-run <run_id>`で詳細（`<run_id>`は前方一致・`latest`エイリアス可）を参照する。`--tool <name>`でdiagnostics全件、`--tool <name> --output`で`output.log`全文が得られる
  - MCPクライアント（Claude Desktopなど）からは`pyfltr mcp`でMCPサーバーを起動する。提供ツールは`list_runs` / `show_run` / `show_run_diagnostics` / `show_run_output` / `run_for_agent`の5種類で、アーカイブ参照と実行を行える

## 注意点

- `uv run mkdocs build --strict`でリンク・nav整合性を検証（ただし日本語アンカーリンク`#見出し日本語`はMkDocs TOCで解決できずINFO通知のみで`--strict`でも検知されないため手動確認要）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は人手同期（SSOT化しない運用）
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する（`run-for-agent`サブコマンド、主要オプションなど）。LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- ドキュメント構成変更時は`docs/development/development.md`の「READMEとdocsの役割分担」節を先に参照
- v3.0.0の実装進捗は`docs/v3/作業ステータス.md`を参照
