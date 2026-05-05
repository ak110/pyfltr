# はじめに

pyfltrを設定して実行するまでの導入手順。
インストール・設定・実行・コーディングエージェント連携・次のステップの順に並ぶ。
既にpyfltrを使っている場合は[CLIコマンド](usage.md)や[設定項目](configuration.md)を参照。

## インストール

推奨は`uvx`での実行。事前のインストールやdev依存への追加は不要で、常に最新のpyfltrを利用できる。

```shell
uvx pyfltr --help
```

`uv`でバージョン管理したい場合は`uv add --dev pyfltr`または`uv add --dev "pyfltr[python]"`で追加し、
`uv run pyfltr ...`で呼び出す。
pip環境では`pip install pyfltr`を使う。

呼び出し方の使い分けと推奨理由は[呼び出し方の使い分け](recommended.md#calling-style)を参照。

## 設定

pyfltrの実行内容は`pyproject.toml`の`[tool.pyfltr]`セクションで指定する。
プリセットと言語カテゴリゲートの2行で足りる。

```toml
[tool.pyfltr]
preset = "latest"
python = true
```

各設定の役割。

- `preset = "latest"`: 各時点での推奨ツール構成のスナップショット。
  Python系一式（ruff-format / ruff-check / mypy / pylint / pyright / pytest / uv-sortなど）を取り込む
- `python = true`: Python系ツールの言語カテゴリゲートを開ける。
  プリセットで`true`になっているツールはこのゲートを通過した分だけ実際に有効化される

JS/TS・Rust・.NETを併用する場合は対応する言語カテゴリキー（`javascript` / `rust` / `dotnet`）を追加する。

```toml
[tool.pyfltr]
preset = "latest"
python = true
javascript = true
```

ドキュメント系（textlint / markdownlint / actionlint / typos / pre-commit）は言語カテゴリゲートに属さず、
プリセットで`true`になっているものがそのまま有効化される。

プリセット・言語カテゴリゲートの詳細は[設定項目](configuration.md)を参照。

## 実行

カレントディレクトリ配下のファイルを対象にチェックを実行する。

```shell
# ローカル開発用：自動修正あり、formatter差分は成功扱い
uvx pyfltr run

# CI・コミット前用：自動修正なし、formatter差分も失敗扱い
uvx pyfltr ci

# pre-commitフック向け軽量チェック：重いツール（mypy / pylint / pytest等）を除外
uvx pyfltr fast
```

特定ツールだけ実行したい場合は`--commands`で限定する。

```shell
uvx pyfltr run --commands=ruff-check src/
uvx pyfltr run --commands=mypy,pyright path/to/file.py
```

サブコマンドの違いと全オプションは[CLIコマンド](usage.md)を参照。

## コーディングエージェントから使う

pyfltrはJSON Lines出力（`--output-format=jsonl`）とMCPサーバー（`pyfltr mcp`）でコーディングエージェント運用に対応する。

### 直接呼び出し（推奨）

エージェントがシェルコマンドを実行できる環境では`pyfltr run-for-agent`を直接呼ぶ。
JSONL出力をそのまま読み込める。

```shell
uvx pyfltr run-for-agent
```

### MCP経由

`pyfltr mcp`でMCPサーバーを起動すると、エージェントが`run_for_agent`等のMCPツールとして呼び出せる。

```shell
# Claude Codeへの登録例
claude mcp add pyfltr -- uvx pyfltr mcp
```

提供するMCPツールやJSONL出力の解釈方法は[CLIコマンド](usage.md#jsonl)の「コーディングエージェント連携」を参照。

## 次のステップ

利用シナリオに応じて次のページを参照。

- 設定項目の全体像と詳細 → [設定項目](configuration.md) /
  [設定項目（ツール別）](configuration-tools.md)
- 推奨設定例（pyproject.toml・pre-commit・タスクランナー・CI）
    - Pythonプロジェクト → [推奨設定例](recommended.md)
    - 非Pythonプロジェクト（TypeScript/JS・Rust・.NET）→
      [推奨設定例（非Pythonプロジェクト）](recommended-nonpython.md)
- カスタムコマンドの追加 → [カスタムコマンド例](custom-commands.md)
- 対応ツール一覧と各ツールの位置づけ → [対応ツール](index.md)
- トラブルが起きたとき → [トラブルシューティング](troubleshooting.md)
