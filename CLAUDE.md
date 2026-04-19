# CLAUDE.md: pyfltr

## 開発手順

- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- リリース手順: [docs/development/development.md](docs/development/development.md) 参照
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- コミット前の検証方法: `uv run pyfltr run-for-agent`
  - ドキュメントなどのみの変更の場合は省略可（pre-commitで実行されるため）
  - テストコードの単体実行なども極力 `uv run pyfltr run-for-agent <path>` を使う（pytestを直接呼び出さない）
    - 詳細な情報などが必要な場合に限り `uv run pytest -vv <path>` などを使用
  - 修正後の再実行時は、対象ファイルや対象ツールを必要に応じて絞って実行する（最終検証はCIに委ねる前提）
    - 例: `pyfltr run-for-agent --commands=mypy,ruff-check path/to/file`

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。実装を変更する際はこの設計判断を崩さないこと。

- root（system logger）: 常にstderr。抑止しない。設定エラー・アーカイブ初期化失敗などを流す
- `pyfltr.textout`: 人間向けテキスト出力（進捗・`write_log`・summary・warnings・`--only-failed`案内）。
  `pyfltr.cli.configure_text_output(stream, *, level)`でformat別にstream/levelを切り替える:
  - `text` / `github-annotations` → stdout / INFO
  - `jsonl` + stdout → stderr / WARN
  - `sarif` + stdout → stderr / INFO
  - `code-quality` + stdout → stderr / INFO
  - `jsonl` / `sarif` / `code-quality` + `--output-file`指定 → stdout / INFO
  - MCP経路（`run_pipeline(force_text_on_stderr=True)`）→ stderr / INFO
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。`pyfltr.cli.configure_structured_output(dest)`で
  `StreamHandler(sys.stdout)`または`FileHandler(output_file)`に切り替える。
  `text` / `github-annotations`ではhandler未設定（構造化出力なし）

出力フォーマット分岐は`pyfltr/formatters.py`の`OutputFormatter` Protocol実装群に集約している（`FORMATTERS`レジストリから動的解決）。

JSONLはstdout / file両モードともcompletion順streamingに統一する。
stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

## 注意点

- `uv run mkdocs build --strict`でリンク・nav整合性を検証（ただし日本語アンカーリンク`#見出し日本語`は
  MkDocs TOCで解決できずINFO通知のみで`--strict`でも検知されないため手動確認要）
- 内部リンクは英数アンカーを優先する。MkDocs（Material）のslugifyは英数のみを採用してアンカー生成する。
  markdownlint MD051は見出し原文を見るため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は
  人手同期（SSOT化しない運用）
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する
 （`run-for-agent`サブコマンド、主要オプションなど）。LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- ドキュメント構成変更時は`docs/development/development.md`の「READMEとdocsの役割分担」節を先に参照
- JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）
