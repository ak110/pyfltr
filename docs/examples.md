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
pyright = true
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
  - repo: https://github.com/DavidAnson/markdownlint-cli2
    rev: v0.21.0
    hooks:
      - id: markdownlint-cli2

  - repo: local
    hooks:
      - id: textlint
        name: textlint
        entry: textlint
        language: node
        files: \.(md|mdown|markdown)$
        args: []
        require_serial: false
        additional_dependencies:
          - textlint@15.5.1
          - textlint-rule-preset-ja-technical-writing@12.0.2
          - textlint-rule-preset-japanese@10.0.4

  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uv run pyfltr --commands=fast
        types: [python]
        require_serial: true
        language: system
```

## CI

```yaml
  - uv install --no-interaction
  - uv run pyfltr
```
