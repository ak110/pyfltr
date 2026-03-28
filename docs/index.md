# pyfltr

Python Formatters, Linters, and Testers Runner

[![CI](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Pythonの各種ツールをまとめて呼び出すツール。

## 対応ツール

- Formatters
    - pyupgrade
    - autoflake
    - isort
    - black
    - ruff format (既定では無効)
    - ruff check --fix (既定では無効)
- Linters
    - pflake8 + flake8-bugbear + flake8-tidy-imports
    - mypy
    - pylint
    - pyright (既定では無効、`pip install pyfltr[pyright]`でインストール可能)
- Testers
    - pytest

## コンセプト

- 各種ツールをまとめて呼び出したい (時間節約のために並列で)
- 各種ツールのバージョンにはできるだけ依存したくない (ので設定とかは面倒見ない)
- exclude周りは各種ツールで設定方法がバラバラなのでできるだけまとめて解消したい (のでpyfltr側で解決してツールに渡す)
- blackやisortはファイルを修正しつつエラーにもしたい (CIとかを想定) (pyupgradeはもともとそういう動作)
- 設定はできるだけ`pyproject.toml`にまとめる

## インストール

```shell
pip install pyfltr
# pip install pyfltr[pyright]  # pyrightを使う場合
```
