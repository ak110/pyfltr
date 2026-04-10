# 設定例

## pyproject.toml

pyfltr本体の設定（`[tool.pyfltr]`）と、呼び出される各ツール（ruff / mypy / pytest）の設定を1つの`pyproject.toml`にまとめた例。

- `preset = "latest"`: 主要ツールを有効化するプリセット。詳細は[設定](configuration.md)を参照。
- `pylint-args`: pylintに追加で渡す引数。`--load-plugins=pylint_pydantic`と`--enable-error-code=unused-awaitable`（mypy）は自動オプションで既定有効のため個別指定不要。
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
        entry: uv run --frozen pyfltr fast
        types_or: [python, markdown]
        require_serial: true
        language: system
```

ポイント:

- `--frozen`: `uv run`が依存解決を再実行せず`uv.lock`をそのまま使うようにする。サプライチェーン攻撃対策として、`git commit`の起動経路がシェル環境変数（`UV_FROZEN`）に依存しなくても確実にfrozen動作させるための保険。
- `fast`: mypy / pylint / pytestなど重いコマンドを除外した高速サブセット。Formatterがファイルを修正しただけではフックを失敗と判定しない。pre-commitは対話的フックのため速度を優先する。
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
pyfltrは`pyfltr fix`実行時にfix段階の起動コマンドから`--format`ペアを自動除去するため。
ただし新規設定では`textlint-lint-args`に書くことを推奨する。

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
                run: uv run pyfltr ci

            - name: Prune uv cache for CI
                run: uv cache prune --ci
```

ポイント:

- `env.UV_FROZEN: "1"`: サプライチェーン攻撃対策として、ワークフロー全体で`uv sync`/`uv run`が`uv.lock`を尊重するよう強制する。意図しない再resolveでロックファイルが書き換わるリスクを抑える。
- `actions/setup-node` + `pnpm/action-setup`: `markdownlint-cli2` と `textlint` をpnpx経由で呼び出すため、PythonだけでなくNode.js環境も必要になる。
- `uv sync --all-extras --all-groups`: pyfltrを含むdev依存をすべて同期し、`uv run pyfltr` から対応ツール群を解決できるようにする。`UV_FROZEN=1`下でも`uv.lock`をそのまま使うため問題なく動作する。
- `uv cache prune --ci`: CIキャッシュを軽量化するための後処理。
