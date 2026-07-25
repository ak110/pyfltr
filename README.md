# pyfltr: 多言語プロジェクトの品質チェック基盤

[![CI][ci-badge]][ci-url]
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

[ci-badge]: https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg
[ci-url]: https://github.com/ak110/pyfltr/actions/workflows/ci.yaml

多言語プロジェクトの品質チェックを一元管理し、コーディングエージェントから扱える形で提供する基盤。
Python / Rust / .NET / TypeScript・JavaScript / ドキュメントなどの
formatter・linter・testerを単一コマンドで実行する。
プロジェクト固有のカスタムチェックも同一基盤へ統合できる。
（要Python 3.11以上）

## 特徴

- プロジェクト固有のカスタムチェック（独自スクリプト等）を組み込みツールと同一の実行基盤へ統合できる
- コーディングエージェント向けJSON Lines出力（`--output-format=jsonl`）とMCPサーバー（`pyfltr mcp`）を提供する
- formatter・linter・testerをまとめて呼び出し、複数ツールの並列実行で総実行時間を短縮する
- 設定を`pyproject.toml`に集約する
- 除外指定（exclude）の書式差をツール間で吸収する
- 横断検索（`grep`）と置換（`replace`、世代管理付き`--undo`対応）を提供する
- シェル補完スクリプトを生成する

## インストール

`uvx pyfltr`で実行する。事前インストールやdev依存への追加なしで最新版を取得して実行する。

```shell
uvx pyfltr --help
```

`uv`でバージョン管理する場合は`uv add --dev pyfltr`または`uv add --dev "pyfltr[python]"`で追加し、
`uv run pyfltr ...`で呼び出す。
pip環境では`pip install pyfltr`を使う。

実行するツールはpyproject.tomlの`[tool.pyfltr]`セクションで指定する。
段階的な導入手順は[はじめに](docs/guide/getting-started.md)を参照。

## 使い方

チェック実行（`ci` / `run` / `fast`）・エージェント向け出力（`run-for-agent`）・横断検索と置換（`grep` / `replace`）など。
実行履歴参照（`list-runs` / `show-run`）・設定操作（`config`）・MCPサーバー（`mcp`）も利用できる。

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

プロジェクト固有のチェックを追加する方法は
[プロジェクト固有チェックの追加](docs/guide/custom-commands.md)を参照。

## ドキュメント

- <https://ak110.github.io/pyfltr/>: はじめに・対応ツール一覧・設定リファレンス
- <https://ak110.github.io/pyfltr/llms.txt>: LLM向け構造化インデックス
- [docs/development/development.md](docs/development/development.md): 開発者向け情報
