# pyfltr: Python Formatters, Linters, and Testers Runner

[![CI][ci-badge]][ci-url]
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

[ci-badge]: https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg
[ci-url]: https://github.com/ak110/pyfltr/actions/workflows/ci.yaml

Python / Rust / .NET / TypeScript・JavaScript / ドキュメントなど多言語プロジェクトの
formatter・linter・testerを単一コマンドで並列実行するCLIツール。
（要Python 3.11以上）

## 特徴

- formatter・linter・testerをまとめて呼び出す
- 複数ツールの並列実行による総実行時間の短縮
- コーディングエージェント向けJSON Lines出力（`--output-format=jsonl`）
- 設定の集約: `pyproject.toml`に集約した統一設定
- 除外指定（exclude）の書式差をツール間で吸収
- MCPサーバー（`pyfltr mcp`）
- シェル補完スクリプト生成

## インストール

推奨は`uvx`での実行。事前のインストールやdev依存への追加は不要で、常に最新のpyfltrを利用できる。

```shell
uvx pyfltr --help
```

`uv`でバージョン管理したい場合は`uv add --dev pyfltr`または`uv add --dev "pyfltr[python]"`で追加し、
`uv run pyfltr ...`で呼び出す。
pip環境では`pip install pyfltr`を使う。

実行するツールはpyproject.tomlの`[tool.pyfltr]`セクションで指定する。
段階的な導入手順は[はじめに](docs/guide/getting-started.md)を参照。

## 使い方

チェック実行（`ci` / `run` / `fast`）・エージェント向け出力（`run-for-agent`）・
実行履歴参照（`list-runs` / `show-run`）・設定操作（`config`）・MCPサーバー（`mcp`）など。

詳細は[CLIコマンド](docs/guide/usage.md)を参照。

### コーディングエージェント向け

`pyfltr run-for-agent`をエージェントから直接呼び出すか、`pyfltr mcp`でMCPサーバーとして登録する。

```shell
# 直接呼び出し（JSONL出力）
uvx pyfltr run-for-agent

# MCPサーバーとして登録（Claude Code例）
claude mcp add pyfltr -- uvx pyfltr mcp
```

詳細は[CLIコマンド](docs/guide/usage.md)の「コーディングエージェント連携」を参照。

## ドキュメント

- <https://ak110.github.io/pyfltr/> — はじめに・対応ツール一覧・設定リファレンス
- <https://ak110.github.io/pyfltr/llms.txt> — LLM向け構造化インデックス
- [docs/development/development.md](docs/development/development.md) — 開発者向け情報
