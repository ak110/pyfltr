# pyfltr: Python Formatters, Linters, and Testers Runner

[![Code style: black](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Lint&Test](https://github.com/ak110/pyfltr/actions/workflows/python-app.yml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/python-app.yml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Pythonの各種ツールをまとめて呼び出すツール。

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

## 主な使い方

### 通常

```shell
pyfltr [files and/or directories ...]
```

対象を指定しなければカレントディレクトリを指定したのと同じ扱い。

指定したファイルやディレクトリの配下のうち、pytest以外は`*.py`のみ、pytestは`*_test.py`のみに対して実行される。

終了コード:

- 0: Formattersによるファイル変更無し、かつLinters/Testersでのエラー無し
- 1: 上記以外

`--exit-zero-even-if-formated`を指定すると、Formattersによるファイル変更があっても
Linters/Testersでのエラー無しなら終了コードは0になる。

### 特定のツールのみ実行

```shell
pyfltr --commands=pyupgrade,autoflake,isort,black,ruff-format,ruff-check,pflake8,mypy,pylint,pyright,pytest [files and/or directories ...]
```

カンマ区切りで実行するツールだけ指定する。

以下のエイリアスも使用可能。(例: `--commands=fast`)

- `format`: `pyupgrade`, `autoflake`, `isort`, `black`, `ruff-format`
- `lint`: `ruff-check`, `pflake8`, `mypy`, `pylint`, `pyright`
- `test`: `pytest`
- `fast`: `pyupgrade`, `autoflake`, `isort`, `black`, `ruff-format`, `ruff-check`, `pflake8`

※ 後述の`pyproject.toml`の`[tool.pyfltr]`で無効になっているコマンドは無視される。

## 設定

`pyproject.toml`で設定する。

### 例

```toml
[tool.pyfltr]
preset = "latest"
pyupgrade-args = ["--py38-plus"]
pylint-args = ["--jobs=4"]
extend-exclude = ["foo", "bar.py"]
```

### 設定項目

設定項目と既定値は`pyfltr --generate-config`で確認可能。

- preset : プリセット設定(後述)
- {command} : 各コマンドの有効/無効
- {command}-path : 実行するコマンド
- {command}-args : 追加のコマンドライン引数
- exclude : 除外するファイル名/ディレクトリ名パターン(既定値あり)
- extend-exclude : 追加で除外するファイル名/ディレクトリ名パターン(既定値は空)

### プリセット設定

`preset`を設定することで、一括して設定を変更できる。

または

```toml
[tool.pyfltr]
preset = "latest"
```

または

```toml
[tool.pyfltr]
preset = "20250710"
```

これらのプリセットは、以下の設定を自動的に適用する：

- `pyupgrade = false`
- `autoflake = false`
- `pflake8 = false`
- `isort = false`
- `black = false`
- `ruff-format = true`
- `ruff-check = true`

`preset = "latest"`は予告なく動作を変更する可能性あり。

## 各種設定例

### pyproject.toml

```toml
[dependency-groups]
dev = [
    ...
    "pyfltr",
]

...

[tool.pyfltr]
preset = "latest"
pyright = true
pylint-args = ["--jobs=4"]

[tool.ruff]
# https://docs.astral.sh/ruff/configuration/
line-length = 128

[tool.ruff.lint]
# https://docs.astral.sh/ruff/linter/#rule-selection
select = [
    # pydocstyle
    "D",
    # pycodestyle
    "E",
    # Pyflakes
    "F",
    # pyupgrade
    "UP",
    # flake8-bugbear
    "B",
    # flake8-simplify
    "SIM",
    # flake8-import-conventions
    "ICN",
    # isort
    "I",
]
ignore = [
    "D107", # Missing docstring in `__init__`
    "D415", # First line should end with a period
]

[tool.ruff.lint.pydocstyle]
convention = "google"

[tool.ruff.lint.per-file-ignores]
"**_test.py" = ["D"]
"**/__init__.py" = ["D104"]  # Missing docstring in public package

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
addopts = "--showlocals -p no:cacheprovider --maxfail=5 --durations=30 --durations-min=0.5"
log_level = "DEBUG"
xfail_strict = true
asyncio_mode = "strict"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

### .pre-commit-config.yaml

```yaml
  - repo: local
    hooks:
      - id: system
        name: pyfltr
        entry: uv run pyfltr --commands=fast
        types: [python]
        require_serial: true
        language: system
```

### CI

```yaml
  - uv install --no-interaction
  - uv run pyfltr
```
