# カスタム指示（プロジェクト固有）

- `pyproject.toml` の編集は極力 `uv` コマンドを使う（`uv add`, `uv remove` など）
  - 手動編集は `uv` コマンドでは対応できない箇所に限る

## 開発手順

- `make format`: 整形 + 軽量lint + 自動修正（開発時の手動実行用）
- `make test`: 全チェック実行（これが成功すればコミットしてよい）
- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- `make docs`: ドキュメントのローカルプレビュー
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- テストコードの実行は `uv run pyfltr <path>` を使う（pytestを直接呼び出さない）
  - `-vv`などが必要な場合に限り `uv run pyfltr -vv <path>` のようにする
- Markdownファイルのformat/lintの実行方法： `uv run pre-commit run --files <file>`
- ドキュメントのみの変更（`*.md`や`docs/**`の更新）をコミットする場合、事前の手動`make test`は省略してよい。`git commit`時点で`pre-commit`の`pyfltr fast`フックが`markdownlint-fast`と`textlint-fast`を自動実行するため、Markdownの検証はそこで担保される
- コードやテストに手を入れた変更では従来どおり`make test`を通してからコミットする

## 依存関係の方針

- サプライチェーン攻撃対策として`UV_FROZEN=1`を`Makefile`とCIワークフローで常時有効化し、`uv sync`/`uv run`が`uv.lock`を再resolveせずそのまま使うようにしている
  - 開発者のシェルでは`UV_FROZEN`を設定しない前提のため、依存の追加・更新は通常どおり`uv add`/`uv remove`/`uv lock --upgrade-package`を使えばよい
  - `make update`も内部で自動的にUV_FROZENを外すため、そのまま実行してよい
  - 詳細な運用方針は`docs/development/development.md`の「UV_FROZENによるlockfile尊重」セクションを参照

## 関連ドキュメント

- @README.md
- @docs/index.md
- @docs/development/development.md
- ドキュメント追加時は `mkdocs.yml` の `nav` を更新要
