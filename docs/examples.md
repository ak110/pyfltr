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
pylint-args = ["--jobs=4"]
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

## CI

```yaml
- name: Setup Node.js
  uses: actions/setup-node@v6
  with:
    node-version: "lts/*"

- name: Setup pnpm
  uses: pnpm/action-setup@v5

- name: Install dependencies
  run: uv sync --all-extras --dev

- name: Test with pyfltr
  run: uv run pyfltr
```
