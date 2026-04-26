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

## 対応ツールの依存方針

pyfltrが対応するformatter/linter/testerの依存指定は以下の基準で振り分ける。
ここでの「公式」は対象ツール本家プロジェクトが直接配布しているPyPIパッケージを指す
（GitHubの本家organization配下から配布されているものを含む）。
さらに「自己完結」とは、wheelにバイナリ等が同梱されており`pip install`時に外部ネットワーク取得を発生させないことを指す。

- 本体依存（`dependencies`）: 本家公式かつ自己完結なPyPIパッケージで、
  Pythonプロジェクト以外でも汎用的に有用なもの（例: `typos`、`pre-commit`）
- python extras（`[python]`）: 本家公式のPyPIパッケージだがPythonプロジェクト専用のもの
 （例: `ruff`、`mypy`、`pylint`、`pyright`、`ty`、`pytest`、`uv-sort`）
- 依存指定なし: 上記いずれにも該当しないもの。
  Node.js系・Goバイナリ・Rust/.NET系など利用者が個別に導入する

サードパーティの非公式PyPIラッパー（例: `shfmt-py`・`actionlint-py`・`shellcheck-py`）は、
本家から独立した個人または別組織のメンテに依存するため本体依存には組み込まない。
本家公式であってもインストール時に外部バイナリを取得するパッケージ（例: `editorconfig-checker`）は、
オフライン・プロキシ環境での導入失敗リスクを避けるため本体依存から除外する。
Node.js等のランタイムを伴うパッケージも、ランタイム導入とサプライチェーンの広さの観点から本体依存に含めない。

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。
実装を変更する際はこの設計判断を崩さないこと。

format別のstream/level切替の詳細は[docs/development/jsonl-output.md](docs/development/jsonl-output.md)を参照。

- root（system logger）: 常にstderr。抑止しない。設定エラー・アーカイブ初期化失敗などを流す
- `pyfltr.textout`: 人間向けテキスト出力。`pyfltr.cli.configure_text_output(stream, *, level)`で切り替える
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。`pyfltr.cli.configure_structured_output(dest)`で切り替える

stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

## 注意点

- 内部リンクは英数アンカーを優先する。MkDocs（Material）のslugifyは英数のみを採用してアンカー生成するため、
  日本語アンカーリンク`#見出し日本語`はTOCで解決できずINFO通知のみで`--strict`でも検知されない（手動確認要）。
  markdownlint MD051は見出し原文を見るため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は
  人手同期（SSOT化しない運用）
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する
 （`run-for-agent`サブコマンド、主要オプションなど）。LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）
