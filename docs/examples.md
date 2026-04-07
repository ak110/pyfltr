# 設定例

## pyproject.toml

pyfltr 本体の設定 (`[tool.pyfltr]`) と、呼び出される各ツール (ruff / mypy / pytest) の設定を1つの`pyproject.toml`にまとめた例。

- `preset = "latest"`: ruff-format / ruff-check / pyright / markdownlint / textlint を有効化するプリセット。詳細は[設定](configuration.md)を参照。
- `pylint-args` / `mypy-args`: 各ツールに追加で渡す引数。プラグイン読み込みや error-code 有効化の典型例を示している。
- ruff の `per-file-ignores`: テストコード (`**_test.py`) と package init (`__init__.py`) の docstring 要求を除外する実用的な調整。

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

ポイント:

- `--exit-zero-even-if-formatted`: Formatter がファイルを修正しただけではフックを失敗扱いにしないためのオプション。pre-commit の通常運用 (修正→再ステージ→再実行) を壊さずに済む。
- `--commands=fast`: mypy / pylint / pytest など重いコマンドを除外した高速サブセット。pre-commit は対話的フックなので速度を優先する。
- `types_or: [python, markdown]`: Python だけでなく Markdown 変更時もフックを起動し、markdownlint / textlint をかける。
- `require_serial: true`: pyfltr 自身が内部で並列化するため、pre-commit 側での多重起動を避ける。

## .markdownlint-cli2.yaml

markdownlint-cli2 が読み込む設定ファイル。日本語ドキュメントでは行長制限が実用的でないため、`line-length` チェックのみ無効化している。

```yaml
config:
  line-length: false
```

## .textlintrc.yaml

textlint の `preset-ja-technical-writing` を有効化しつつ、実運用で引っかかりがちな `ja-no-mixed-period` (句読点の混在) と `sentence-length` (1文の長さ) を無効化する例。

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
    "textlint", "--format", "compact",
]
```

## Claude Code Hook

[Claude Code](https://docs.claude.com/en/docs/claude-code/overview) で開発する場合、
編集ターン終了時に pyfltr の `--commands=fast` を自動実行する hook を設定すると便利。

設計上のポイントは次の通り。

- PostToolUse で即整形しない: Claude が import を追加→次の編集で使用、という段階的な
  編集の途中で `ruff check --fix` が未使用 import を消してしまう問題を避けるため、
  整形は Stop hook (応答完了時) に集約する。
- マーカーファイルで編集対象を限定: ユーザーが手で編集中の Python ファイルが
  ある状態で、Claude に質問しただけでも整形が実行されるのを避けるため、PostToolUse で
  編集されたファイルパスをマーカーファイルに追記し、Stop hook はそのマーカーに
  記録されたファイルに対してのみ pyfltr を実行する。
- SessionStart でマーカーをクリア: 前回セッションで異常終了した場合の残骸を除去する。
- Stop では存在確認と重複排除: マーカー内のファイルパスは重複や削除済みの可能性が
  あるため、`sort -u` と `[ -e ]` で整理してから実行する。

`.claude/settings.json` の例:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "rm -f \"$CLAUDE_PROJECT_DIR/.claude/.format-dirty\"; exit 0"
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "f=$(jq -r '.tool_input.file_path // empty'); case \"$f\" in *.py) mkdir -p \"$CLAUDE_PROJECT_DIR/.claude\" && printf '%s\\n' \"$f\" >> \"$CLAUDE_PROJECT_DIR/.claude/.format-dirty\" ;; esac; exit 0"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "M=\"$CLAUDE_PROJECT_DIR/.claude/.format-dirty\"; [ -s \"$M\" ] || { rm -f \"$M\"; exit 0; }; cd \"$CLAUDE_PROJECT_DIR\" || exit 0; sort -u \"$M\" -o \"$M\"; awk 'NF' \"$M\" | while IFS= read -r f; do [ -e \"$f\" ] && printf '%s\\n' \"$f\"; done > \"$M.tmp\" && mv \"$M.tmp\" \"$M\"; [ -s \"$M\" ] || { rm -f \"$M\"; exit 0; }; xargs -a \"$M\" -d '\\n' uv run pyfltr --exit-zero-even-if-formatted --commands=fast >&2; rm -f \"$M\"; exit 0"
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
.claude/.format-dirty
```

## CI

GitHub Actions で pyfltr を Python バージョンの matrix で実行する構成の例。

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

ポイント:

- `actions/setup-node` + `pnpm/action-setup`: `markdownlint-cli2` と `textlint` を pnpx 経由で呼び出すため、Python だけでなく Node.js 環境も必要になる。
- `uv sync --all-extras --all-groups`: pyfltr を含む dev 依存をすべて同期し、`uv run pyfltr` から対応ツール群を解決できるようにする。
- `uv cache prune --ci`: CI キャッシュを軽量化するための後処理。
