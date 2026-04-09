# pyfltr: Python Formatters, Linters, and Testers Runner

[![CI](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Pythonの各種ツールをまとめて呼び出すツール。

ドキュメント： <https://ak110.github.io/pyfltr/>

llms.txt: <https://ak110.github.io/pyfltr/llms.txt>

## 対応ツール

- Formatters: pyupgrade / autoflake / isort / black / ruff format (+ ruff check --fix --unsafe-fixes)
- Linters: ruff check / pflake8 / mypy / pylint / pyright / ty / markdownlint-cli2 / textlint
- Testers: pytest

## インストール

```shell
pip install pyfltr
```

## 基本的な使い方

```shell
pyfltr [files and/or directories ...]
```

詳細は[ドキュメント](https://ak110.github.io/pyfltr/)を参照。
