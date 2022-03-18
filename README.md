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
    - pflake8 + flake8-bugbear
    - mypy
    - pylint
- Testers
    - pytest

## コンセプト

- 各種ツールをまとめて呼び出したい
- 各種ツールのバージョンにはできるだけ依存したくない (ので設定とかは面倒見ない)
- exclude周りは各種ツールで設定方法がバラバラなのでできるだけまとめて解消したい (のでpyfltr側で解決してツールに渡す)
- blackやisortはファイルを修正しつつエラーにもしたい (CIとかを想定) (pyupgradeはもともとそういう動作)
- Q: pysenでいいのでは？ A: それはそう

## インストール

```shell
$ pip install pyfltr
```

## 主な使い方

### 通常

```shell
$ pyfltr [files and/or directories ...]
```

対象を指定しなければカレントディレクトリを指定したのと同じ扱い。

指定したファイルやディレクトリの配下のうち、pytest以外は`*.py`のみ、pytestは`*_test.py`のみに対して実行される。

終了コード:

- 0: Formattersによるファイル変更無し、かつLinters/Testersでのエラー無し
- 1: 上記以外

### 特定のツールのみ実行

```shell
$ pyfltr --commands=pyupgrade,isort,black,pflake8,mypy,pylint,pytest [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。

## 設定

`pyproject.toml`で設定する。

### 例

```toml
[tool.pyfltr]
pyupgrade-args = ["--py38-plus"]
pylint-args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

### 設定項目

設定項目と既定値は`pyfltr --generate-config`で確認可能。

- {command} : コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数
- exclude : 除外するファイル名/ディレクトリ名パターン(既定値あり)
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン(既定値は空)

## 各種設定例

### pyproject.toml

```toml
[tool.poetry.dev-dependencies]
pyfltr = "*"

[tool.pyfltr]
pyupgrade-args = ["--py38-plus"]
pylint-args = ["--jobs=4"]

[tool.isort]
# https://black.readthedocs.io/en/stable/guides/using_black_with_other_tools.html#isort
# https://pycqa.github.io/isort/docs/configuration/options.html
profile = "black"

[tool.black]
# https://black.readthedocs.io/en/stable/usage_and_configuration/the_basics.html
target-version = ['py38']
skip-magic-trailing-comma = true

[tool.flake8]
# https://black.readthedocs.io/en/stable/guides/using_black_with_other_tools.html#flake8
# https://flake8.pycqa.org/en/latest/user/configuration.html
max-line-length = 88
extend-ignore = "E203,"

[tool.mypy]
# https://mypy.readthedocs.io/en/stable/config_file.html
allow_redefinition = true
check_untyped_defs = true
ignore_missing_imports = true
strict_optional = true
strict_equality = true
warn_no_return = true
warn_redundant_casts = true
warn_unused_configs = true
show_error_codes = true

[tool.pytest.ini_options]
# https://docs.pytest.org/en/latest/reference/reference.html#ini-options-ref
addopts = "--showlocals -p no:cacheprovider"
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

### CI

```yaml
  - poetry install --no-interaction
  - poetry run pyfltr
```
