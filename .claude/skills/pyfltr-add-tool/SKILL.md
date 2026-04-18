---
name: pyfltr-add-tool
description: pyfltr に新しい formatter / linter / tester を追加する際の定型手順チェックリスト。config.py / command.py / error_parser.py / docs/guide/index.md / tests を一貫して更新する。
---

# pyfltr 新ツール追加チェックリスト

pyfltrに新しいformatter / linter / testerを追加するときの作業項目を、変更箇所が漏れないように列挙する。
更新箇所は最低6か所に分散しているため、以下を上から順に対応すること。

## 0. 前提情報の整理

新ツールについて以下を決める。

- **ツール名**（`pyproject.toml` に書く識別子。`-` 区切り推奨。例: `ruff-format`）
- **type**: `formatter` / `linter` / `tester` のいずれか
- **対象拡張子**: `*.py` か、`*.md` のような別のglobか
- **fast 該当か**: `--commands=fast`（= `make format`）で実行する軽量ツールか（判断基準は後述の「fast判定の計測手順」を参照）
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

### 1-3. 言語カテゴリ定数（`config.py` 上部）

新ツールが特定言語専用の場合、対応する定数タプルに追加する。

```python
PYTHON_COMMANDS: tuple[str, ...] = (
    ...
    "<new-tool>",
)
```

カテゴリ: `PYTHON_COMMANDS`・`JAVASCRIPT_COMMANDS`・`RUST_COMMANDS`・`DOTNET_COMMANDS`。
カテゴリに属するツールは `python`/`javascript`/`rust`/`dotnet` キーが `False` のときゲートで無効化される。
全言語共通のツール（`typos`・`ec`・`actionlint` 等）はいずれのカテゴリにも属さない。

### 1-4. プリセット（`load_config` 内）

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

## 6. ドキュメント

- `docs/guide/index.md` の「対応ツール」一覧に追記する

対応ツール一覧は`docs/guide/index.md`に一元化されている。`README.md`には書かない。

## 7. 検証

```bash
uv run pyfltr run-for-agent
```

このテストが成功すればコミット可能。

## 参照する既存実装

雛形にすべき既存ツールは `pyfltr/command.py` と `pyfltr/error_parser.py` を参照:

- formatterの例: `ruff-format` / `prettier`
- linterの例: `ruff-check` / `mypy`
- testerの例: `pytest`（ほぼ唯一なので新規testerは要相談）

## fast判定の計測手順

`{command}-fast`のデフォルト値を決める際は、以下の手順で実行時間を計測し、ユーザーに判断材料を提示する。

### 方針

`fast`はpre-commitフックなどで実行しても作業に支障が出にくい高速なツールを意味する。判断基準は固定コスト（起動オーバーヘッド）と可変コスト（ファイルあたりの処理時間）の両方に加え、ツールの重要度や性質も考慮する。最終判断はユーザーが行う。

### 計測方法

複数プロジェクトで少数ファイルと全ファイルの2パターンを計測し、`T = a + b * N`（a: 固定コスト、b: ファイルあたりコスト）を推定する。

```bash
# 少数ファイル（対象種別のファイルを1〜2個指定）
uv run pyfltr run-for-agent <file1> [file2] 2>/dev/null

# 全ファイル（引数なしでリポジトリ全体を対象）
uv run pyfltr run-for-agent 2>/dev/null
```

JSONL出力の`tool`レコードから`tool`, `elapsed`, `files`を抽出する:

```bash
... | python3 -c "
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    if r.get('kind') == 'tool':
        print(f\"{r['tool']:20s} {r['elapsed']:7.2f}s  files={r['files']}\")"
```

推定式: `b = (T_all - T_small) / (N_all - N_small)`, `a = T_small - b * N_small`

`pass-filenames=False`のツール（cargo系、dotnet系、tsc等）はファイル数によらずプロジェクト全体を走査するため、固定コストのみとして扱う。

### 参考計測値（2026-04-13、ウォーム状態）

| ツール | 固定コスト a | 可変コスト b (s/file) | 現状fast | 備考 |
| -------- | :---: | :---: | :---: | ------ |
| ruff-format | ~0.02s | ~0 | True | |
| ruff-check | ~0.01s | ~0 | True | |
| ty | ~0.05s | ~0.01 | True | |
| typos | ~0.04s | ~0.005-0.009 | True | |
| uv-sort | ~0.2s | N/A | True | 単一ファイル対象 |
| actionlint | ~0.1-0.3s | ~0 | True | |
| shfmt | ~0s | ~0 | True | |
| shellcheck | ~0s | ~0.03 | True | |
| oxlint | ~0.7s | ~0 | True | |
| markdownlint | ~0.9s | ~0.02-0.05 | True | pnpx起動コスト |
| prettier | ~1.5s | ~0.02 | True | pnpx起動コスト |
| textlint | ~1.6-3.0s | ~0.2-0.6 | True | 重いが意識しにくいルールを検出する重要度から採用 |
| eslint | ~2.3s | ~0.05 | False | |
| mypy | ~0.2s | ~0.004-0.23 | False | 可変コストがプロジェクト依存で不安定 |
| pylint | ~1-2.5s | ~0.2-0.4 | False | 固定・可変とも高い |
| pyright | ~0.5-1.1s | ~0.13-0.18 | False | |
| pytest | - | - | False | テスト実行 |
| vitest | - | - | False | テスト実行 |

計測対象: pytilpack（148py/10md）、pyfltr（22py/15md）、dotfiles（53py/38md）、glatasks（70ts/13md）。
