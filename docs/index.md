# pyfltr

Python Formatters, Linters, and Testers Runner

[![CI](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml/badge.svg)](https://github.com/ak110/pyfltr/actions/workflows/ci.yaml)
[![PyPI version](https://badge.fury.io/py/pyfltr.svg)](https://badge.fury.io/py/pyfltr)

Pythonの各種ツールをまとめて呼び出すツール。

ドキュメント: <https://ak110.github.io/pyfltr/>

llms.txt: <https://ak110.github.io/pyfltr/llms.txt>

## 対応ツール

- Formatters
    - pyupgrade
    - autoflake
    - isort
    - black
    - ruff format（既定では無効。有効時は `ruff check --fix --unsafe-fixes` を併走する、`ruff-format-by-check`でOFF可）
    - prettier（既定では無効、`js-runner`設定で起動方式を切替可能。`--check`と`--write`の2段階実行）
- Linters
    - ruff check（既定では無効）
    - pflake8 + flake8-bugbear + flake8-tidy-imports
    - mypy
    - pylint
    - pyright（既定では無効）
    - ty（既定では無効）
    - markdownlint-cli2（既定では無効、`js-runner`設定で起動方式を切替可能。既定は`pnpx`）
    - textlint（既定では無効、`js-runner`設定で起動方式を切替可能。`textlint-packages`でプリセット/ルール指定）
    - eslint（既定では無効、`js-runner`設定で起動方式を切替可能。`--format json`で機械可読出力を取得）
    - biome（既定では無効、`js-runner`設定で起動方式を切替可能。`biome check`サブコマンドと`--reporter=github`を使用）
- Testers
    - pytest

## コンセプト

- 各種ツールをまとめて並列で呼び出し、実行時間を短縮する
- 各種ツールのバージョンには極力依存しない（各ツール固有の設定には対応しない）
- excludeの指定方法が各ツールで異なる問題を、pyfltr側で解決してツールに渡すことで吸収する
- blackやisortはファイルを修正しつつエラーとしても扱う（CI用途などを想定。pyupgradeは本来そのような動作）
- 設定は極力`pyproject.toml`に集約する

## インストール

```shell
pip install pyfltr
```
