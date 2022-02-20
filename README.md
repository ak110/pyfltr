# pyfltr: Python Formatters, Linters, and Testers Runner.

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Lint&Test](https://github.com/ak110/pyfltr/actions/workflows/python-app.yml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/python-app.yml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Pythonの各種ツールをまとめて呼び出すツール。

- Formatters
    - pyupgrade
    - isort
    - black
- Linters
    - pflake8
    - mypy
    - pylint
- Testers
    - pytest

## 動作詳細

- pytestの対象ファイルは`*_test.py`、pytest以外の対象ファイルは`*.py`
- 終了コード:
    - 0: Formattersによるファイル変更無し、かつLinters/Testersでのエラー無し
    - 1: 上記以外

## コンセプト

- 各種ツールをまとめて呼び出したい
- 各種ツールのバージョンにはできるだけ依存したくない (ので設定とかは面倒見ない)
- exclude周りは各種ツールで設定方法がバラバラなのでできるだけまとめて解消したい (のでpyfltr側で解決してツールに渡す)
- blackやisortはファイルを修正しつつエラーにもしたい (CIとかを想定) (pyupgradeはもともとそういう動作)
- Q: pysenでいいのでは？ A: それはそう。

## 主な使い方

### 通常

```text
$ pyfltr [files and/or directories ...]
```

対象を指定しなければカレントディレクトリを指定したのと同じ扱い。

`*.py`などを指定した場合も、pytestは`*_test.py`に対してのみ実行される。

### 特定のツールのみ実行

```text
$ pyfltr --commands=pyupgrade,isort,black,pflake8,mypy,pylint,pytest [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。

## 設定

`pyproject.toml`で設定する。

### 例

```toml
[tool.pyfltr]
pyupgrade_args = ["--py38-plus"]
pylint_args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

### 設定項目

設定項目と既定値は`pyfltr --generate-config`で確認可能。

- {command} : コマンドの有効/無効
- {command}_path : 実行するコマンド
- {command}_args : 追加のコマンドライン引数
- exclude : 除外するファイル名パターン(既定値あり)
- extend-exclude : 除外するファイル名パターン(既定値は空)

## 各種設定例

### pyproject.toml

```toml
...

[tool.poetry.dev-dependencies]
pyfltr = "*"

[tool.pyfltr]
pyupgrade_args = ["--py38-plus"]
pylint_args = ["--jobs=4"]

...
```

### .pre-commit-config.yaml

```yaml
  - repo: local
    hooks:
      - id: system
        name: pyfltr
        entry: poetry run pyfltr --commands=pyupgrade,isort,black,pflake8
        types: [python]
        require_serial: true
        language: system
```

### CI例

```yaml
  - poetry install --no-interaction
  - poetry run pyfltr
```
