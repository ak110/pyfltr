# pyfltr: Python Formatters, Linters, and Testers Runner

<!-- markdownlint-disable-next-line MD013 -->
[![CI](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Python / Rust / .NET / TypeScript・JavaScript / ドキュメントなど多言語プロジェクトの
formatter・linter・testerを単一コマンドで並列実行するCLIツール。
（要Python 3.11以上）

## 特徴

- formatter・linter・testerをまとめて呼び出す
- 複数ツールの並列実行による総実行時間の短縮
- コーディングエージェント向けJSON Lines出力（`--output-format=jsonl`）
- 設定の集約: `pyproject.toml`に寄せた統一設定
- 除外指定（exclude）の書式差をツール間で吸収
- MCPサーバー（`pyfltr mcp`）
- シェル補完スクリプト生成

## インストール

```shell
pip install pyfltr
# uv を使う場合
uv add --dev pyfltr
```

uvxでの実行も可能。

```shell
uvx pyfltr --help
```

Python系ツール（ruff / mypy / pylint / pyright / ty / pytest / uv-sort）を利用する場合は、extrasで追加依存を導入する。

```shell
pip install 'pyfltr[python]'
```

Python / JavaScript / Rust / .NETの各言語カテゴリに属するツールはすべて既定で無効（opt-in）である。
`preset = "latest"` + 言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）の`true`指定だけで、
当該言語の推奨ツール一式が有効化される。
プリセット非収録のツール（`ty`など）を追加したい場合のみ個別に`{command} = true`を指定する。
詳細は[設定項目](docs/guide/configuration.md)を参照。

## コーディングエージェント向け運用

コーディングエージェントから利用する経路は2種類ある。

直接呼び出し（シェルコマンドが実行できる環境）:

```shell
# JSONL形式で全チェックを実行し、結果をエージェントが読み込む
pyfltr run-for-agent

# 直前runで失敗したツール・ファイルだけ再実行
pyfltr run-for-agent --only-failed
```

`run-for-agent`は`pyfltr run --output-format=jsonl`のエイリアス。
JSONL出力の末尾`summary`行で全体像を把握でき、必要に応じて`diagnostic`行を参照することでトークン消費を抑えられる。

MCP経由（コーディングエージェントのMCPクライアント経由）:

```shell
# コーディングエージェントへの登録例（コマンド形式）
claude mcp add pyfltr -- pyfltr mcp
```

または設定ファイルに直接記載する方法:

```json
{
  "mcpServers": {
    "pyfltr": {
      "command": "pyfltr",
      "args": ["mcp"]
    }
  }
}
```

MCPツール`run_for_agent`で`paths`・`commands`・`fail_fast`を指定して実行できる。

## ドキュメント

- <https://ak110.github.io/pyfltr/> — 概要・対応ツール一覧・設定リファレンス
- <https://ak110.github.io/pyfltr/llms.txt> — LLM向け構造化インデックス
- [docs/development/development.md](docs/development/development.md) — 開発者向け情報
