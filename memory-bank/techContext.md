# 技術コンテキスト: pyfltr

## 技術スタック

### 言語とバージョン

- Python 3.10以上
- 型ヒントの活用
- モダンなPython機能の使用

### 主要な依存関係

```toml
dependencies = [
    "autoflake>=2.0",     # 未使用import/変数の削除
    "black>=22.0",        # コードフォーマッター
    "dill>=0.3",         # オブジェクトのシリアライズ
    "flake8-bugbear>=23.0", # コード品質チェック
    "isort>=5.0",        # import文の整理
    "joblib>=1.3",       # 並列処理
    "mypy>=1.0",         # 型チェック
    "pylint>=3.0",       # コード分析
    "pyproject-flake8>=7.0", # flake8の設定管理
    "pytest>=7.0",       # テストフレームワーク
    "pyupgrade>=3.0",    # Python構文の最新化
    "tomli>=2.0",        # TOML設定ファイルの読み込み
]
```

### 開発ツール

- uv: パッケージ管理
- pre-commit: コミット前チェック
- GitHub Actions: CI/CD
- hatchling: ビルドシステム
- hatch-vcs: バージョン管理

## 開発環境設定

### パッケージ管理

```bash
# uvによるパッケージインストール
uv pip install -e ".[dev]"
```

### pre-commit設定

```yaml
- repo: local
  hooks:
    - id: system
      name: pyfltr
      entry: poetry run pyfltr --commands=fast
      types: [python]
      require_serial: true
      language: system
```

### プロジェクト設定

- pyproject.tomlによる一元管理
- 各ツールの設定統合
- バージョン管理との連携

## ビルドと配布

### ビルドシステム

- hatchlingをバックエンドとして使用
- ソースからのバージョン生成
- 依存関係の適切な管理

### パッケージング

- PyPIへの公開
- wheel形式でのビルド
- 依存関係の適切な指定

## テスト環境

### テストフレームワーク

- pytestによるテスト実行
- pytest-mockによるモック機能
- キャッシュ無効化オプション

### CI環境

- GitHub Actionsでの自動化
- 複数Pythonバージョンでのテスト
- コードカバレッジの計測

## 制約と要件

### パフォーマンス要件

- 効率的な並行処理
- 最小限のメモリ使用
- 高速な実行時間

### 互換性要件

- Python 3.10以上のサポート
- 各OSでの動作保証
- 依存パッケージの互換性維持

### セキュリティ要件

- 安全なファイル操作
- 適切な権限管理
- 依存関係の脆弱性チェック

## 監視と計測

### エラー監視

- 明確なエラーメッセージ
- スタックトレースの提供
- 実行ステータスの追跡

### パフォーマンス計測

- 実行時間の計測
- リソース使用量の監視
- ボトルネックの特定

## 今後の技術的課題

### 最適化

- 並行処理の改善
- メモリ使用の最適化
- 実行速度の向上

### 拡張性

- 新しいツールの追加
- 設定オプションの拡充
- インターフェースの改善
