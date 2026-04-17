# pyfltr: Python Formatters, Linters, and Testers Runner

[![CI](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Python / Rust / .NET / TypeScript・JavaScript / ドキュメントなど多言語プロジェクトのformatter・linter・testerを単一コマンドで並列実行するCLIツール。

## 特徴

- 多言語対応のformatter・linter・testerをまとめて呼び出す単一コマンド
- 複数ツールの並列実行による総実行時間の短縮
- 設定の集約: `pyproject.toml`に寄せた統一設定
- 除外指定（exclude）の書式差をツール間で吸収
- 自動修正系ツール（ruff format・prettierなど）を修正と失敗扱いの両立で実行
- LLMエージェント向けJSON Lines出力（`pyfltr run-for-agent`・`PYFLTR_OUTPUT_FORMAT`環境変数・`--output-format=jsonl`）に対応
- シェル補完スクリプト生成（`pyfltr generate-shell-completion`）に対応

## インストール

```shell
pip install pyfltr
```

Python系ツール（ruff / mypy / pylint / pyright / ty / pytest / uv-sort）を利用する場合は、extrasで追加依存を導入する。

```shell
pip install 'pyfltr[python]'
```

v3.0.0以降、Python系ツールは既定で無効（opt-in）となり、`pyproject.toml`に`python = true`または`{command} = true`を明示した場合のみ実行される。
詳細は[設定項目](docs/guide/configuration.md)を参照。

## ドキュメント

- <https://ak110.github.io/pyfltr/> — 概要・対応ツール一覧・設定リファレンス
- <https://ak110.github.io/pyfltr/llms.txt> — LLM向け構造化インデックス
- [docs/development/development.md](docs/development/development.md) — 開発者向け情報
