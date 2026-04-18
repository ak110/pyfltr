# v3.0.0マイグレーションガイド

pyfltr v2.xからv3.0.0への移行手順をチェックリスト形式で示す。
v3.0.0は破壊的変更を多く含む大型リリースのため、項目を順に確認する。

v3.0.0以降の変更履歴は本ドキュメントでは管理しない。
個々の変更点は[git log](https://github.com/ak110/pyfltr/commits/master)および[GitHub Releases](https://github.com/ak110/pyfltr/releases)を参照する。

## 1. サブコマンドを明示指定する

v2.xまでは`pyfltr`を引数なしで実行すると`ci`として動作した。
v3.0.0ではサブコマンドが必須となる。

```shell
# 旧: サブコマンド省略可
pyfltr

# 新: サブコマンド必須
pyfltr ci
```

使用可能なサブコマンドは次の通り（詳細は[CLIコマンド](usage.md)を参照）。

- `ci` — CIモード（従来の既定挙動）
- `run` — 通常実行（formatter変更を成功扱い + fixステージ有効）
- `fast` — 高速ツールのみ
- `run-for-agent` — LLMエージェント向け（JSONL出力既定）
- `list-runs` / `show-run` — 実行アーカイブの参照
- `mcp` — MCPサーバー起動
- `generate-config` — 設定雛形出力
- `generate-shell-completion <shell>` — 補完スクリプト出力

Makefile・CI設定・pre-commit設定などで`pyfltr`をサブコマンドなしで呼び出している箇所があれば、すべて明示指定に書き換える。

## 2. プリセット`"20250710"`を置き換える

プリセット`"20250710"`は削除された。

```toml
# 旧
[tool.pyfltr]
preset = "20250710"

# 新
[tool.pyfltr]
preset = "latest"
# または具体的な日付指定プリセットを使う
# preset = "20260413"
```

`preset = "20250710"`を指定したまま実行すると、案内付きの設定エラーが出る。

## 3. 削除ツールの設定キーを撤去する

以下5ツールが削除された。

- `pyupgrade`
- `autoflake`
- `isort`
- `black`
- `pflake8`

削除理由はruffへの統合で代替可能となり、プリセット`20250710`以降は既定で無効化されていたため。
保守対象として長期間コードベースに残り続ける負債を断ち切る意図で、v3.0.0の破壊的変更に合わせて一括削除とした。

`[tool.pyfltr]`に関連する設定キー（`pyupgrade = true` / `black-args = [...]` / `isort-path = "..."`等）が残っている場合は、すべて削除する。
設定ファイル読込時に該当キーを検知すると、対象ツール名を明示したエラーを出す。

ruff / ruff-formatへの移行例。

```toml
# 旧: black + isort を使用
[tool.pyfltr]
black = true
isort = true

# 新: ruff-format + ruff-check で代替（preset = "latest" で自動有効化）
[tool.pyfltr]
preset = "latest"
python = true
```

## 4. Python系ツールを明示的に有効化する

v3.0.0ではPython系ツール（`mypy` / `pylint` / `pyright` / `ty` / `pytest` / `ruff-format` / `ruff-check` / `uv-sort`）がopt-in化された。
非Pythonプロジェクトで意図しないツール実行を招くことを避けるため、既定値をすべて`False`に変更している。

Pythonプロジェクトで利用する場合は、次のいずれかの方法で明示的に有効化する。

### 方法A: 一括有効化

```toml
[tool.pyfltr]
python = true
```

`python = true`を指定すると、プリセットで定義されたPython系ツールがまとめて有効になる。

### 方法B: 個別有効化

```toml
[tool.pyfltr]
mypy = true
pytest = true
ruff-format = true
ruff-check = true
```

必要なツールだけを有効化する。
適用優先度は`preset < python < 個別設定`。

非Pythonプロジェクトでは両方を省略すればPython系ツールは一切実行されない。

## 5. Python系の依存を導入する

v3.0.0ではPython系linter / testerが`pyfltr[python]`オプショナルグループに分離された。

```shell
# 旧
pip install pyfltr

# 新（Python系ツールも使う場合）
pip install 'pyfltr[python]'
```

uvを使う場合。

```shell
uv add 'pyfltr[python]'
```

`pyfltr[python]`に含まれる依存は次の通り。

- `dill` / `mypy` / `pre-commit` / `pylint` / `pylint-pydantic`
- `pyright[nodejs]` / `pytest` / `pytest-asyncio` / `ruff` / `ty` / `uv-sort`

非Pythonプロジェクトでは`pyfltr`のみインストールすれば十分。
本体必須依存は`mcp` / `natsort` / `platformdirs` / `python-ulid` / `pyyaml` / `textual`のみで、Python系linterを一切含まない。

## 6. 新機能の活用

v3.0.0で追加された機能のうち、日常利用に影響するものを次に示す。

### 実行アーカイブ（既定で有効）

全実行のツール生出力・diagnostic・メタ情報がユーザーキャッシュ配下に自動保存される。
保存先は`platformdirs.user_cache_dir("pyfltr")`で解決する。
Linuxは`~/.cache/pyfltr/`、macOSは`~/Library/Caches/pyfltr/`、Windowsは`%LOCALAPPDATA%\pyfltr\Cache`。

- `--no-archive`で個別実行時に無効化
- `[tool.pyfltr].archive = false`で恒久的に無効化
- 既定で世代数100・合計1GB・30日のいずれかを超過すると古い順に自動削除

JSONL出力時、`header`レコードに`run_id`（ULID）が付与される。
このフィールドを使って後から`pyfltr show-run <run_id>`または`pyfltr show-run latest`でツール生出力を含む全文参照ができる。
`pyfltr list-runs`でrun一覧も確認できる。

### ファイルhashキャッシュ（既定で有効）

対象ファイル未変更時のtextlint実行をスキップし、過去の結果を復元する（textlintのみ対象）。
キャッシュヒット時はJSONL `tool`レコードに`cached: true` / `cached_from: <ソースrun_id>`が付与される。

- `--no-cache`で個別実行時に無効化
- `[tool.pyfltr].cache = false`で恒久的に無効化
- 既定で12時間経過後に自動削除（`cache-max-age-hours`で調整可能）

### MCPサーバー同梱

`pyfltr mcp`でstdioトランスポートのMCPサーバーが起動する。
Claude Desktop等のMCPクライアントから`list_runs` / `show_run` / `show_run_diagnostics` / `show_run_output` / `run_for_agent`の5ツールが使える。
詳細は[CLIコマンド](usage.md)のmcp節を参照。

### JSONL出力の拡張

`--output-format=jsonl`の出力に次のフィールドが追加された。

- `header.run_id`（ULID）— 実行アーカイブの参照キー
- `diagnostic.rule_url` — 対応ツール（ruff / pylint / pyright / mypy / shellcheck / eslint / markdownlint）のルールドキュメントURL
- `diagnostic.severity` — `error` / `warning` / `info`の3値に正規化
- `tool.retry_command` — 1ツール再実行用のshellコマンド文字列（失敗ファイルのみに絞り込み）
- `tool.truncated` — smart truncation発生時の切り詰め前情報とアーカイブパス
- `tool.cached` / `tool.cached_from` — ファイルhashキャッシュ復元時の判別情報

### 出力形式の追加

`--output-format=sarif`（SARIF 2.1.0互換）と`--output-format=github-annotations`（GitHub Actions向け注釈）が追加された。
GitHub code scanningへの取り込みやプル要求のインライン表示に利用できる。

### `--fail-fast`

1ツールでもエラーが発生した時点で残りのジョブを打ち切る。
起動済みサブプロセスには`terminate()`（最大5秒待機 → `kill()`フォールバック）を送り、未開始ジョブは`future.cancel()`で取消して`skipped`として扱う。

### `--only-failed` / `--from-run`

直前run（または`--from-run`で指定した過去run）の実行アーカイブから失敗ツール・失敗ファイルを抽出し、ツール別にその組み合わせのみを再実行する。

```shell
# 直前runの失敗組み合わせのみ再実行
pyfltr run-for-agent --only-failed

# 特定runを参照
pyfltr run-for-agent --only-failed --from-run 01HXYZ
```

直前runが存在しない・失敗ツールが無い・指定`targets`との交差が空の場合はメッセージを出してrc=0で成功終了する。

## チェックリスト

移行時は以下の順で確認するとスムーズに進む。

- [ ] `pyfltr`コマンド呼び出し箇所すべてにサブコマンド（`ci` / `run` / `fast` / `run-for-agent`）を追記した
- [ ] `pyproject.toml`の`preset = "20250710"`を`"latest"`または`"20260413"`等に置き換えた
- [ ] `pyproject.toml`から`pyupgrade` / `autoflake` / `isort` / `black` / `pflake8`の関連設定キーをすべて削除した
- [ ] Pythonプロジェクトの場合、`python = true`または個別`{command} = true`を追加した
- [ ] Pythonプロジェクトの場合、`pyfltr[python]`で依存を再導入した
- [ ] pre-commit hookの`entry:`フィールドにサブコマンドが含まれていることを確認した
- [ ] Makefile・CIワークフロー・miseタスク等のコマンド呼び出しを再確認した
- [ ] 自動アーカイブを無効化したい場合は`[tool.pyfltr].archive = false`を追加した
