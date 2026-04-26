# v3.0.0マイグレーションガイド

pyfltr v2.xからv3.0.0への移行手順をチェックリスト形式で示す。
v3.0.0は破壊的変更を多く含むメジャーリリースのため、項目を順に確認する。

v3.0.0以降の変更履歴は本ドキュメントでは管理しない。
個々の変更点は[git log](https://github.com/ak110/pyfltr/commits/master)および
[GitHub Releases](https://github.com/ak110/pyfltr/releases)を参照する。

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

## 2. 旧プリセット`"20250710"`を置き換える

preset `"20250710"`はv3.0.0で削除された。指定したまま実行すると移行先を案内する設定エラーが出る。

```toml
# 旧
[tool.pyfltr]
preset = "20250710"

# 新
[tool.pyfltr]
preset = "latest"
python = true
```

`"20260330"` / `"20260411"` / `"20260413"`はそのまま利用できる。
`"latest"`は`"20260413"`を指すエイリアスで、pyfltrの更新に伴って対象ツールの追加や既定値の変更が予告なく入ることがある。
破壊的変更を避けたい場合は日付指定プリセットで固定すると、当該日時点の構成をそのまま維持できる。

## 3. 削除ツールの設定キーを撤去する

以下5ツールが削除された。

- `pyupgrade`
- `autoflake`
- `isort`
- `black`
- `pflake8`

削除理由はruffへの統合で代替可能となり、プリセット`20250710`以降は既定で無効化されていたため。
保守対象として長期間コードベースに残り続ける負債を断ち切る意図で、v3.0.0の破壊的変更に合わせて一括削除とした。

`[tool.pyfltr]`に関連する設定キー（`pyupgrade = true` / `black-args = [...]` / `isort-path = "..."`等）が
残っている場合は、すべて削除する。
設定ファイル読込時に該当キーを検知すると、対象ツール名を明示したエラーを出す。

ruff / ruff-formatへの移行例。

```toml
# 旧: black + isort を使用
[tool.pyfltr]
black = true
isort = true

# 新: ruff-format + ruff-check で代替（preset = "latest" で推奨構成を有効化）
[tool.pyfltr]
preset = "latest"
python = true
```

## 4. 言語カテゴリを明示的に有効化する

v3.0.0ではPython / JavaScript / Rust / .NETの各言語カテゴリに属するツールをすべてopt-in化した。
プリセットは各時点の推奨ツール構成をバージョン付きで示すスナップショットで、全言語の推奨ツールを横断的に収録する。
言語カテゴリキー（`python` / `javascript` / `rust` / `dotnet`）はプリセットが推奨する言語別ツールを通過させるゲートとして働く。

`preset = "latest"` + `{language} = true`の組み合わせだけで当該言語の推奨ツール一式が有効化される。
適用優先度は`preset < 言語カテゴリゲート < 個別設定`で、個別設定がある場合はそれが最優先。

```toml
[tool.pyfltr]
preset = "latest"
python = true
```

カテゴリキーを`true`にすると、プリセット内の該当言語ツールが一式そのまま有効化される。
カテゴリキーを`false`（既定）のままにすると、プリセットで推奨された該当言語ツールはゲートで`false`に押し戻され実行されない。
他言語のプロジェクトへ誤って当該ツールが実行されることを防ぐため。

プリセットに含まれない個別ツール（例: `ty`は現行プリセット非収録）を追加したい場合は`{command} = true`で指定する。
個別指定はゲートを越えて最優先される。

```toml
[tool.pyfltr]
preset = "latest"
python = true
ty = true        # preset に含まれないツールを追加
```

`{command} = false`はプリセットでも有効化されたツールを個別に無効化する用途にも使える。

```toml
[tool.pyfltr]
preset = "latest"
python = true
pyright = false  # preset が True にした pyright を個別に抑止
```

## 5. Python系の依存を導入する

v3.0.0ではPython系linter / testerが`pyfltr[python]`オプショナルグループに分離された。

```shell
# 旧
pip install pyfltr

# 新（Python系ツールも使う場合）
pip install 'pyfltr[python]'
```

uvを使う場合は開発依存として追加する。

```shell
uv add --dev 'pyfltr[python]'
```

`pyfltr[python]`に含まれる依存は次の通り。

- `dill` / `mypy` / `pylint` / `pylint-pydantic`
- `pyright[nodejs]` / `pytest` / `pytest-asyncio` / `ruff` / `ty` / `uv-sort`

非Pythonプロジェクトでは`pyfltr`のみインストールすれば十分。
本体必須依存は`mcp` / `natsort` / `platformdirs` / `pre-commit` / `python-ulid` / `pyyaml` / `textual`で、
Python系linterを一切含まない。
`pre-commit`は言語非依存でどのプロジェクトでも利用できるため常時依存とする。

## 6. v3.0.0以降の新機能

v3.0.0以降の変更履歴は本ドキュメントでは管理しない（冒頭の方針通り）。
主な新機能の入口を以下に示す。

- 実行アーカイブ・ファイルhashキャッシュ → [archive-and-cache.md](../development/archive-and-cache.md)
- MCPサーバー（`pyfltr mcp`）→ [usage.md](usage.md)
- JSONLスキーマ拡張（`command.hint-urls` / `command.retry_command` / smart truncation）
  → [jsonl-output.md](../development/jsonl-output.md)
- `{command}-runner`設定（v3.x系で追加）→ [configuration-tools.md](configuration-tools.md)
- 失敗のみ再実行（`--only-failed` / `--from-run`）→ [usage.md](usage.md)

最新の変更点は[GitHub Releases](https://github.com/ak110/pyfltr/releases)を参照。

## チェックリスト

移行時は以下の順で確認すると円滑に進む。

- [ ] `pyfltr`コマンド呼び出し箇所すべてにサブコマンド（`ci` / `run` / `fast` / `run-for-agent`）を追記した
- [ ] `pyproject.toml`の旧preset名`"20250710"`を`"latest"`または
  日付プリセット（`"20260330"` / `"20260411"` / `"20260413"`）に置き換えた
- [ ] `pyproject.toml`から`pyupgrade` / `autoflake` / `isort` / `black` / `pflake8`の関連設定キーをすべて削除した
- [ ] Pythonプロジェクトの場合、`python = true`でpreset推奨ツール一式のゲートを開けた
- [ ] JavaScript / TypeScriptプロジェクトの場合、`javascript = true`でpreset推奨ツール一式のゲートを開けた
- [ ] Rustプロジェクトの場合、`rust = true`でpreset推奨ツール一式のゲートを開けた
- [ ] `.NET`プロジェクトの場合、`dotnet = true`でpreset推奨ツール一式のゲートを開けた
- [ ] Pythonプロジェクトの場合、`uv add --dev 'pyfltr[python]'`（またはpipの相当コマンド）で依存を再導入した
- [ ] pre-commit hookの`entry:`フィールドにサブコマンドが含まれていることを確認した
- [ ] Makefile・CIワークフロー・miseタスク等のコマンド呼び出しを再確認した
- [ ] 自動アーカイブを無効化したい場合は`[tool.pyfltr].archive = false`を追加した
- [ ] CIワークフローで`--output-format=github-annotations`または`--output-format=sarif`の利用を検討した
