# pyfltr: Python Formatters, Linters, and Testers Runner

[![CI](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Pythonの各種formatter / linter / testerをまとめて並列実行するツール。

## 特徴

- Python向けのformatter・linter・testerをまとめて呼び出す単一コマンド
- 複数ツールの並列実行による総実行時間の短縮
- 設定の集約: `pyproject.toml`に寄せた統一設定
- 除外指定（exclude）の書式差をツール間で吸収
- 自動修正系ツール（pyupgrade・black・isortなど）を修正と失敗扱いの両立で実行

## インストール

```shell
pip install pyfltr
```

## ドキュメント

- <https://ak110.github.io/pyfltr/> — 概要・対応ツール一覧・設定リファレンス
- [llms.txt](https://ak110.github.io/pyfltr/llms.txt) / [llms-full.txt](https://ak110.github.io/pyfltr/llms-full.txt) — LLM向け構造化インデックス
- [docs/development/development.md](docs/development/development.md) — 開発者向け情報
