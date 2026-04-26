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

対応するPython系ツールはruff-format / ruff-check / mypy / pylint / pyright / ty / pytest / uv-sortの8種。
このうちtyのみpreset非収録のため、必要に応じて`ty = true`を個別指定する。
Python系ツールを利用する場合はextrasで追加依存を導入する。

```shell
pip install 'pyfltr[python]'
```

Python / JavaScript / Rust / .NETの各言語カテゴリに属するツールはすべて既定で無効（opt-in）である。
`preset = "latest"` + 言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）の`true`指定だけで、
当該言語の推奨ツール一式が有効化される。
詳細は[設定項目](docs/guide/configuration.md)を参照。

## 使い方

```shell
uvx pyfltr --help
```

uvxでの実行も可能。

## 主なサブコマンド

- `pyfltr ci` / `run` / `fast` — チェック実行（CI / ローカル全体 / 軽量）
- `pyfltr run-for-agent` — エージェント向けJSONL出力（`pyfltr run --output-format=jsonl`のエイリアス）
- `pyfltr list-runs` / `show-run` — 実行履歴の参照
- `pyfltr command-info` — ツール起動方式の確認
- `pyfltr mcp` — MCPサーバー

詳細は[CLIコマンド](docs/guide/usage.md)を参照。

## コーディングエージェント向け運用

`pyfltr run-for-agent`をエージェントから直接呼び出すか、`pyfltr mcp`でMCPサーバーとして登録する。

```shell
# 直接呼び出し（JSONL出力）
uvx pyfltr run-for-agent

# MCPサーバーとして登録（Claude Code例）
claude mcp add pyfltr -- uvx pyfltr mcp
```

詳細は[CLIコマンド](docs/guide/usage.md)の「コーディングエージェント連携」を参照。

## ドキュメント

- <https://ak110.github.io/pyfltr/> — 概要・対応ツール一覧・設定リファレンス
- <https://ak110.github.io/pyfltr/llms.txt> — LLM向け構造化インデックス
- [docs/development/development.md](docs/development/development.md) — 開発者向け情報
