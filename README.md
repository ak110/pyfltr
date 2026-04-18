# pyfltr: Python Formatters, Linters, and Testers Runner

[![CI](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Python / Rust / .NET / TypeScript・JavaScript / ドキュメントなど多言語プロジェクトのformatter・linter・testerを単一コマンドで並列実行するCLIツール。
（要Python 3.11以上）

## 特徴

- 多言語対応のformatter・linter・testerをまとめて呼び出す単一コマンド
- 複数ツールの並列実行による総実行時間の短縮
- 設定の集約: `pyproject.toml`に寄せた統一設定
- 除外指定（exclude）の書式差をツール間で吸収
- 自動修正系ツール（ruff format・prettierなど）を修正と失敗扱いの両立で実行
- LLMエージェント向けJSON Lines出力（`pyfltr run-for-agent`・`PYFLTR_OUTPUT_FORMAT`環境変数・`--output-format=jsonl`）に対応
- MCPサーバーを本体同梱（`pyfltr mcp`）。Claude Desktopなどのエージェントから直接lint実行・アーカイブ参照が行える
- シェル補完スクリプト生成（`pyfltr generate-shell-completion`）に対応

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
`preset = "latest"` + 言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）の`true`指定だけで、当該言語の推奨ツール一式が有効化される。
プリセット非収録のツール（`ty`など）を追加したい場合のみ個別に`{command} = true`を指定する。
詳細は[設定項目](docs/guide/configuration.md)を参照。

## ドキュメント

- <https://ak110.github.io/pyfltr/> — 概要・対応ツール一覧・設定リファレンス
- <https://ak110.github.io/pyfltr/llms.txt> — LLM向け構造化インデックス
- [docs/development/development.md](docs/development/development.md) — 開発者向け情報
