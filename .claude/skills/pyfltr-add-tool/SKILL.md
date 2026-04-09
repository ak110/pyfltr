---
name: pyfltr-add-tool
description: pyfltr に新しい formatter / linter / tester を追加する際の定型手順チェックリスト。config.py / command.py / error_parser.py / README.md / docs/index.md / tests を一貫して更新する。
disable-model-invocation: true
---

# pyfltr 新ツール追加チェックリスト

pyfltrに新しいformatter / linter / testerを追加するときの作業項目を、変更箇所が漏れないように列挙する。
更新箇所は最低6か所に分散しているため、以下を上から順に対応すること。

## 0. 前提情報の整理

新ツールについて以下を決める。

- **ツール名**（`pyproject.toml` に書く識別子。`-` 区切り推奨。例: `ruff-format`）
- **type**: `formatter` / `linter` / `tester` のいずれか
- **対象拡張子**: `*.py` か、`*.md` のような別のglobか
- **fast 該当か**: `--commands=fast`（= `make format`）で実行する軽量ツールか
- **コマンドラインの形**（`{path} {args} {files}` 形式が基本）
- **出力フォーマット**（`error_parser.py` で必要なグループ: `file` / `line` / `message`）

## 1. `pyfltr/config.py`

### 1-1. `BUILTIN_COMMANDS` 辞書（順序が出力順を決める）

```python
BUILTIN_COMMANDS: dict[str, CommandInfo] = {
    ...
    "<new-tool>": CommandInfo(type="<formatter|linter|tester>", targets="*.py"),
    ...
}
```

`targets` がデフォルト（`*.py`）と異なる場合のみ明示。挿入位置は他ツールとの実行順を意識。

### 1-2. `DEFAULT_CONFIG` 辞書

各ツールにつき4つのキーを追加する:

```python
"<new-tool>": True,                  # 既定で有効か
"<new-tool>-path": "<実行パス>",
"<new-tool>-args": ["<デフォルト引数>"],
"<new-tool>-fast": True,             # fast 実行対象か
```

### 1-3. プリセット（`load_config` 内）

`preset = "latest"` 等で既定挙動を変えたい場合、`config.values["<new-tool>"]` を明示的にon/offする。

## 2. `pyfltr/command.py`

新ツール用の実行ロジックを追加する。既存ツールの `_run_xxx` を雛形として複製するのが効率的。
共通化されている部分（`_run_command` 等）があればそれを使い、新規に車輪の再発明をしない。

確認ポイント:

- exit codeの解釈（formatterは「整形あり」を1などで返すケースがある）
- 出力ストリーム（stdout / stderrの混在）
- ファイル数0件のスキップ条件

## 3. `pyfltr/error_parser.py`

ツール出力をエラー情報にパースする正規表現を追加。**必須グループ**: `file`, `line`, `message`。
`tests/error_parser_test.py` に実出力サンプルを用意してテストを追加すること。

## 4. `pyproject.toml`

依存追加は **必ず `uv add` を使用**:

```bash
uv add <new-tool-package>
# optional にする場合
uv add --optional <extra> <new-tool-package>
```

`uv.lock` を直接編集しない（PreToolUse hookでブロックされる）。

## 5. テスト追加（`tests/`）

最低限以下を追加:

- `tests/config_test.py`: `DEFAULT_CONFIG` キーの存在と型を確認
- `tests/command_test.py`: ダミー入力で実行できることを確認（実コマンドが重い場合は最小ケース）
- `tests/error_parser_test.py`: 想定エラー出力をパースできることを確認

## 6. ドキュメント（両方更新）

- `README.md` の「対応ツール」一覧
- `docs/index.md` の「対応ツール」一覧

両方に追記すること。片方だけだとSSOT違反としてCI / レビューで指摘される。

## 7. 検証

```bash
make test
```

このテストが成功すればコミット可能。

## 参照する既存実装

雛形にすべき既存ツールは `pyfltr/command.py` と `pyfltr/error_parser.py` を参照:

- formatterの例: `ruff-format` / `black`
- linterの例: `ruff-check` / `mypy`
- testerの例: `pytest`（ほぼ唯一なので新規testerは要相談）
