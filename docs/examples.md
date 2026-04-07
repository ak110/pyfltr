# 設定例

## pyproject.toml

```toml
[dependency-groups]
dev = [
    ...
    "pyfltr",
]

...

[tool.pyfltr]
preset = "latest"
pylint-args = ["--jobs=4", "--load-plugins=pylint_pydantic"]
mypy-args = ["--enable-error-code=unused-awaitable"]

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

## .pre-commit-config.yaml

```yaml
  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uv run pyfltr --exit-zero-even-if-formatted --commands=fast
        types_or: [python, markdown]
        require_serial: true
        language: system
```

## .markdownlint-cli2.yaml

```yaml
config:
  line-length: false
```

## .textlintrc.yaml

```yaml
rules:
  preset-ja-technical-writing:
    ja-no-mixed-period: false
    sentence-length: false
```

## textlint-argsのカスタマイズ

追加のtextlintプリセットを使う場合は`textlint-args`をオーバーライドする。

```toml
[tool.pyfltr]
textlint-args = [
    "--package", "textlint",
    "--package", "textlint-rule-preset-ja-technical-writing",
    "--package", "textlint-rule-preset-japanese",
    "textlint", "--format", "compact",
]
```

## Claude Code Hook

[Claude Code](https://docs.claude.com/en/docs/claude-code/overview) で開発する場合、
編集ターン終了時に pyfltr の `--commands=fast` を自動実行する hook を設定すると便利。

ポイントは2つ。

- **PostToolUse で即整形しない**: Claude が import を追加→次の編集で使用、という段階的な
  編集の途中で `ruff check --fix` が未使用 import を消してしまう問題を避けるため、
  整形は Stop hook (応答完了時) に集約する。
- **マーカーファイルで Claude 編集ターンに限定**: ユーザーが手で編集中の Python ファイルが
  ある状態で、Claude に質問しただけでも整形が走るのを避けるため、PostToolUse で
  マーカーを置き、Stop hook はそのマーカーがあるときだけ pyfltr を実行する。

`.claude/settings.json` の例 (プロジェクトルート直下のディレクトリ名は適宜調整):

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.tool_input.file_path // empty' | grep -qE '\\.py$' && mkdir -p \"$CLAUDE_PROJECT_DIR/.claude\" && touch \"$CLAUDE_PROJECT_DIR/.claude/.pyfltr-dirty\"; exit 0"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "MARKER=\"$CLAUDE_PROJECT_DIR/.claude/.pyfltr-dirty\"; [ -f \"$MARKER\" ] && { (cd \"$CLAUDE_PROJECT_DIR\" && uv run pyfltr --exit-zero-even-if-formatted --commands=fast <ソースディレクトリ> tests) >&2; rm -f \"$MARKER\"; }; exit 0"
          }
        ]
      }
    ]
  }
}
```

`.gitignore` には以下を追加:

```text
.claude/settings.local.json
.claude/.pyfltr-dirty
```

依存関係の安全性を高めたい場合、`uv.lock` の直接編集を禁止する PreToolUse hook も併用できる:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "jq -r '.tool_input.file_path // empty' | grep -qE '(^|/)(uv\\.lock|\\.venv/)' && { echo 'uv.lock / .venv/ の直接編集は禁止です。`uv add` / `uv remove` を使用してください。' >&2; exit 2; } || exit 0"
          }
        ]
      }
    ]
  }
}
```

## CI

```yaml
jobs:
    test:
        runs-on: ubuntu-latest
        strategy:
            matrix:
                python-version: ["3.11", "3.12", "3.13", "3.14"]
        steps:
            - uses: actions/checkout@v6

            - name: Install uv
                uses: astral-sh/setup-uv@v7
                with:
                    python-version: ${{ matrix.python-version }}
                    enable-cache: true

            - name: Setup Node.js
                uses: actions/setup-node@v6
                with:
                    node-version: "lts/*"

            - name: Setup pnpm
                uses: pnpm/action-setup@v5
                with:
                    version: latest

            - name: Install dependencies
                run: uv sync --all-extras --all-groups

            - name: Test with pyfltr
                run: uv run pyfltr

            - name: Prune uv cache for CI
                run: uv cache prune --ci
```
