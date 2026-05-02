# CLAUDE.md: pyfltr

Python/Rust/.NET/TypeScript・JavaScript/ドキュメントなど多言語プロジェクトの
formatter・linter・testerを単一コマンドで並列実行するCLIツール。
JSON Lines出力（`--output-format=jsonl`）とMCPサーバー（`pyfltr mcp`）でコーディングエージェント運用にも対応する。

## 開発手順

- `make update`: 依存更新 + pre-commit autoupdate + pinactアクション更新 + 全テスト実行
  - `make update-actions`: GitHub Actionsのハッシュピン更新のみ（mise経由でpinact実行）
- リリース手順: [docs/development/development.md](docs/development/development.md) 参照
- テストコードは`pyfltr/xxx_.py`に対して`tests/xxx_test.py`として配置する
- コミット前の検証方法: `uv run --with-editable=. pyfltr run-for-agent`
  - 自リポでは「ローカル編集中のpyfltrを反映する」目的で`--with-editable=.`を使う。
    他プロジェクト向けの利用者推奨は`uvx pyfltr ...`であり、本リポのみ限定例外として扱う
  - ドキュメントなどのみの変更の場合は省略可（pre-commitで実行されるため）
  - テストコードの単体実行なども極力pyfltrを使う（pytestを直接呼び出さない）。
    具体的には `uv run --with-editable=. pyfltr run-for-agent <path>` 等
  - 修正後の再実行時は、対象ファイルや対象ツールを必要に応じて絞って実行する（最終検証はCIに委ねる前提）
    - 例: `uv run --with-editable=. pyfltr run-for-agent --commands=mypy,ruff-check path/to/file`

## アーキテクチャの参照先

サブパッケージ・モジュールごとの構成詳細とサブパッケージ間の依存方向、
format別のlogger stream/level切替の詳細は[docs/development/architecture.md](docs/development/architecture.md)を参照する。

## 注意点

- ツール解決経路（`uv`／`mise`／`direct`／`js-runner`等）と本体依存方針はトピック別の規約ファイルに集約している。
  新ツール追加や依存方針を変更する際は規約ファイル群とdocs側のSSOTを合わせて見直す
- JSONLスキーマ・logger分離・SSOT・テスト実装制約・subprocess PATH整理方針も同様に
  自動ロード対象の規約ファイル群へ分離している。
  実装変更時はそれらの設計判断を崩さない
- 利用者向けの推奨呼び出し方は`uvx pyfltr`（最新解決）。
  `uv add --dev "pyfltr[python]"` + `uv run pyfltr`の運用も選べるが、
  両者の使い分けと推奨理由のSSOTは`docs/guide/recommended.md`の「呼び出し方の使い分け」節
- ドッグフーディング方針として、対応ツールは可能な限り本リポで有効化する。
  対象外は「入力ファイルが本リポジトリに存在しないツール」と「外部依存でCI安定度を著しく下げるツール」のみ
