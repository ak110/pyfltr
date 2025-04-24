# 技術スタック

## 主要な依存関係

- Python 3.10以上
- uv (パッケージ管理)
- pre-commit (コミット時の自動チェック)

## 使用ツール

### Formatters

- pyupgrade: Pythonコードを新しい文法に更新
- autoflake: 未使用のインポートと変数を削除
- isort: インポート文のソート
- black: コードフォーマッター

### Linters

- pflake8 + flake8-bugbear: コードスタイルチェック
- mypy: 型チェック
- pylint: 静的解析

### Testers

- pytest: ユニットテスト
- pytest-mock: テストモック

## 開発環境設定

- pyproject.tomlでツールの設定を管理
- Makefileで一般的なタスクを自動化
- GitHub Actionsでの自動テスト
- pre-commitフックでコミット時の品質チェック

## パッケージング

- hatchling + hatch-vcsによるビルドシステム
- PyPIへの公開設定

## 設定管理

- pyproject.tomlで一元管理
- 各ツールの設定をプロジェクトに最適化
- blackスタイルをベースにした一貫性のある整形ルール
