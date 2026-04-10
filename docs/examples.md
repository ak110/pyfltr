# 設定例

## pyproject.toml

pyfltr本体の設定（`[tool.pyfltr]`）と、呼び出される各ツール（ruff / mypy / pytest）の設定を1つの`pyproject.toml`にまとめた例。

- `preset = "latest"`: ruff-format / ruff-check / pyright / markdownlint / textlintを有効化するプリセット。詳細は[設定](configuration.md)を参照。
- `pylint-args` / `mypy-args`: 各ツールに追加で渡す引数。プラグイン読み込みやerror-code有効化の典型例を示している。
- ruffの `per-file-ignores`: テストコード（`**_test.py`）とpackage init（`__init__.py`）のdocstring要求を除外する実用的な調整。

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

### JS/TSプロジェクトでの推奨設定

JS/TSを併用するプロジェクトでは、`js-runner`をプロジェクトのパッケージマネージャーに合わせることを推奨する。
既定の`pnpx`はツールを都度取得するため、CIで毎回ダウンロードが発生する。
`pnpm`や`npm`など、プロジェクトで使用しているパッケージマネージャーを指定すれば、`package.json`で管理済みのパッケージを再利用できる。

```toml
[tool.pyfltr]
js-runner = "pnpm"
```

`pnpm` / `npm` / `yarn` / `direct`では`textlint-packages`は無視される（`package.json`側でインストールする前提のため）。
textlintのプリセットやルールも`package.json`の`devDependencies`で管理すること。

詳細は[docs/configuration.md](configuration.md)の「npm系ツール」を参照。

## .pre-commit-config.yaml

```yaml
  - repo: local
    hooks:
      - id: pyfltr
        name: pyfltr
        entry: uv run --frozen pyfltr --exit-zero-even-if-formatted --commands=fast
        types_or: [python, markdown]
        require_serial: true
        language: system
```

ポイント:

- `--frozen`: `uv run`が依存解決を再実行せず`uv.lock`をそのまま使うようにする。サプライチェーン攻撃対策として、`git commit`の起動経路がシェル環境変数（`UV_FROZEN`）に依存しなくても確実にfrozen動作させるための保険。
- `--exit-zero-even-if-formatted`: Formatterがファイルを修正しただけではフックを失敗と判定しないためのオプション。pre-commitの通常運用（修正→再ステージ→再実行）を阻害せずに済む。
- `--commands=fast`: mypy / pylint / pytestなど重いコマンドを除外した高速サブセット。pre-commitは対話的フックのため速度を優先する。
- `types_or: [python, markdown]`: PythonだけでなくMarkdown変更時もフックを起動し、markdownlint / textlintを実行する。
- `require_serial: true`: pyfltr自身が内部で並列化するため、pre-commit側での多重起動を避ける。

## .markdownlint-cli2.yaml

markdownlint-cli2が読み込む設定ファイル。日本語ドキュメントでは行長制限が実用的でないため、`line-length` チェックのみ無効化している。

```yaml
config:
  line-length: false
```

## .textlintrc.yaml

textlintで技術文書向けの複数プリセットと誤用語チェックを併用する例。
`preset-ja-technical-writing` / `preset-jtf-style` / `ja-no-abusage` を組み合わせる。
原則はデフォルトルールに従い、誤検出や技術文書に合わないルールのみ個別に無効化する。

無効化しているルールと理由は以下の通り。

- `ja-no-mixed-period`: ラベル型見出し（「ポイント」「例」など）が多用されるため
- `no-doubled-joshi`: 技術文書で避けられない自然な助詞連結が頻出するため
- `sentence-length`: 既定の100文字制限を120文字へ緩和する（完全無効化はしない）
- `no-mix-dearu-desumasu`: 本文・リストを常体（である調）に固定する（プリセット既定は本文が敬体）
- `1.1.3.箇条書き`: 箇条書きに句点を付けない方針のため
- `4.2.7.コロン(：)`: コロン終端のラベル記法を多用するため

対応する`textlint-packages`の設定例は[textlint-packagesのカスタマイズ](#textlint-packagesのカスタマイズ)を参照。

```yaml
rules:
  preset-ja-technical-writing:
    ja-no-mixed-period: false
    no-doubled-joshi: false
    sentence-length:
      max: 120
    no-mix-dearu-desumasu:
      preferInHeader: ""
      preferInBody: "である"
      preferInList: "である"
      strict: false
  preset-jtf-style:
    "1.1.3.箇条書き":
      shouldUsePoint: false
    "4.2.7.コロン(：)": false
  ja-no-abusage: true
```

## textlint-packagesのカスタマイズ

追加のtextlintプリセットを使う場合は `textlint-packages` にパッケージ名を列挙する（pnpx / npx起動時に `--package` / `-p` として展開される）。

```toml
[tool.pyfltr]
textlint-packages = [
    "textlint-rule-preset-ja-technical-writing",
    "textlint-rule-preset-jtf-style",
    "textlint-rule-ja-no-abusage",
]
```

共通のコマンドライン引数を追加したい場合は `textlint-args` を使う。
lint専用のオプション（`--format compact` など）は `textlint-lint-args` に分離する。

```toml
[tool.pyfltr]
textlint-args = []
textlint-lint-args = ["--format", "compact"]
```

旧版の`textlint-args = ["--format", "compact", ...]`をそのまま引き継いでもクラッシュしない。
pyfltrは`--fix`実行時にfix段階の起動コマンドから`--format`ペアを自動除去するため。
ただし新規設定では`textlint-lint-args`に書くことを推奨する。

## Claude Code Hook

[Claude Code](https://docs.claude.com/en/docs/claude-code/overview)で開発する場合、
編集ターン終了時にpyfltrの `--commands=fast` を自動実行するhookを設定するとよい。

設計上のポイントは次の通り。

- PostToolUseで即整形しない: 整形はStop hook（応答完了時）に集約する。
  Claudeがimportを追加して次の編集で使用する段階的な編集の途中で、
  `ruff check --fix` が未使用importとして削除してしまう問題を避けるため。
- マーカーファイルで編集対象を限定: PostToolUseで編集されたファイルパスを
  マーカーファイルに追記し、Stop hookはそのマーカーに記録されたファイルに対してのみ
  pyfltrを実行する。
  これにより、ユーザーが手動で編集中のPythonファイルがある状態で
  Claudeに質問しただけでも整形が実行されるのを避ける。
- SessionStartでマーカーをクリア: 前回セッションで異常終了した場合の残存ファイルを除去する。
- Stopでは存在確認と重複排除: マーカー内のファイルパスは重複や削除済みの可能性が
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

GitHub ActionsでpyfltrをPythonバージョンのmatrixで実行する構成の例。

```yaml
env:
    # サプライチェーン攻撃対策: uvがlockfileを常に尊重し、意図しない再resolveを防ぐ
    UV_FROZEN: "1"

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

- `env.UV_FROZEN: "1"`: サプライチェーン攻撃対策として、ワークフロー全体で`uv sync`/`uv run`が`uv.lock`を尊重するよう強制する。意図しない再resolveでロックファイルが書き換わるリスクを抑える。
- `actions/setup-node` + `pnpm/action-setup`: `markdownlint-cli2` と `textlint` をpnpx経由で呼び出すため、PythonだけでなくNode.js環境も必要になる。
- `uv sync --all-extras --all-groups`: pyfltrを含むdev依存をすべて同期し、`uv run pyfltr` から対応ツール群を解決できるようにする。`UV_FROZEN=1`下でも`uv.lock`をそのまま使うため問題なく動作する。
- `uv cache prune --ci`: CIキャッシュを軽量化するための後処理。
