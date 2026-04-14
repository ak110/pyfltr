# カスタム指示（プロジェクト固有）

## 開発手順

- `pyproject.toml` の編集は極力 `uv` コマンドを使う（`uv add`, `uv remove` など）
  - 手動編集は `uv` コマンドでは対応できない箇所に限る
- `make format`: 整形 + 軽量lint + 自動修正（開発時の手動実行用）
- `make test`: 全チェック実行（これを通過すればコミット可能）
- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- `make docs`: ドキュメントのローカルプレビュー
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- 実行パイプラインの構造: `run_pipeline`（main.py）がTUI/非TUI分岐の最上位関数。パイプライン共通の前処理（ファイル展開など）はこの関数内でTUI起動前に実行する
- テストコードの実行は `uv run pyfltr <path>` を使う（pytestを直接呼び出さない）
  - `-vv`などが必要な場合に限り `uv run pyfltr -vv <path>` のようにする
- Markdownファイルのformat/lintの実行方法: `uv run pre-commit run --files <file>`
- ドキュメントのみの変更（`*.md`や`docs/**`の更新）をコミットする場合、事前の手動`make test`は省略してよい。`git commit`時点で`pre-commit`の`pyfltr fast`フックが`markdownlint-fast`と`textlint-fast`を自動実行するため、Markdownの検証はそこで担保される
- コードやテストに手を入れた変更では従来どおり`make test`を通してからコミットする

## Claude Code向けコミット前検証

Claude Codeがコミット前に検証する際は、`make test`の代わりに以下を実行する。JSON Lines出力によりLLMがツール別診断を効率的に解釈できる。

```bash
uv run pyfltr run --output-format=jsonl
```

人間の開発者は従来どおり`make test`を使用する。

## 依存関係の方針

- サプライチェーン攻撃対策として`UV_FROZEN=1`を`Makefile`とCIワークフローで常時有効化し、`uv sync`/`uv run`が`uv.lock`を再resolveせずそのまま使うようにしている
  - 開発者のシェルでは`UV_FROZEN`を設定しない前提のため、依存の追加・更新は通常どおり`uv add`/`uv remove`/`uv lock --upgrade-package`を使えばよい
  - `make update`も内部で自動的にUV_FROZENを外すため、そのまま実行してよい
  - 詳細な運用方針は`docs/development/development.md`の「サプライチェーン攻撃対策」セクションを参照

## ドキュメント編集時の注意

- `uv run mkdocs build --strict`でリンク・nav整合性を検証（ただし日本語アンカーリンク`#見出し日本語`はMkDocs TOCで解決できずINFO通知のみで`--strict`でも検知されないため手動確認要）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は人手同期（SSOT化しない運用）
- ドキュメント構成変更時は`docs/development/development.md`の「READMEとdocsの役割分担」節を先に参照

## 関連ドキュメント

- @README.md
- @docs/index.md
- @docs/guide/index.md
- @docs/development/index.md
- @docs/development/development.md
- ドキュメント追加時は `mkdocs.yml` の `nav` を更新要
