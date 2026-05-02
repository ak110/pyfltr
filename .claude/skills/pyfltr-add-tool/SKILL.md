---
name: pyfltr-add-tool
description: >-
  pyfltr に新しい formatter / linter / tester を追加する際の定型手順チェックリスト。
  command/builtin.py / config/config.py / command/dispatcher.py / command/error_parser.py / docs/guide/index.md / tests を一貫して更新する。
---

# pyfltr 新ツール追加チェックリスト

新規ツール追加では複数ファイルへの整合した変更が必要になる。
各ファイルの記述方法は既存ツールを雛形にすれば把握できるため、本書は「触るべきファイル」と
「コードを読んだだけでは気付きにくい注意点」のみを列挙する。

## 触るべきファイル

雛形にする既存ツールを1つ決め（用途が近いもの）、その変更箇所をすべて踏襲する。

- `pyfltr/command/builtin.py`: `BUILTIN_COMMANDS` への登録（順序が実行順と出力順を決める）。
  特定言語専用ツールはあわせて `PYTHON_COMMANDS` 等の言語カテゴリ定数にも追加する
- `pyfltr/config/config.py`: `DEFAULT_CONFIG` への設定キー追加と、`aliases` への登録
- `pyfltr/command/dispatcher.py`（および必要に応じて `pyfltr/command/` 配下の関連モジュール）: 実行ロジック。共通ヘルパーを優先利用し、独自経路は最小限に抑える
- `pyfltr/command/error_parser.py`: 出力パーサー（regexまたは関数ベース）
- `tests/`: `config_test.py`・`command_*_test.py`・`error_parser_test.py` に対応するテストを追加
- `docs/guide/index.md`:「対応ツール」一覧へ追記（`README.md`には書かない。SSOTは本ファイル）
- `docker/Dockerfile`: 公式Dockerイメージ (`ghcr.io/ak110/pyfltr`) は対応ツールを事前同梱する方針のため、
  新ツールも該当する導入経路のRUN層へ追加する。経路の選び方は冒頭コメントの「同梱ツール一覧」を参照する
 （bin-runner系は `mise use --global`、JS系は `pnpm add -g`、Python単独は `uv tool install`）。
  Rust / .NETツールチェイン依存のものは同梱対象外

## 気付きにくい注意点

- `aliases` の `format` / `lint` / `test` への登録を忘れると、`--commands=lint` 等で対象から漏れる
- bin-runner対応ツール（miseバックエンド経由のネイティブバイナリ）は、通常の4キーに加えて
  `-version` キーを必須とし、`-path` の既定値は空文字列にする
- `error_parser` のカスタム関数パーサーは `_CUSTOM_PARSERS` 辞書に登録しないと有効化されない
- 依存追加は `uv add` を使う（`uv.lock` の直接編集はPreToolUse hookでブロックされる）

## 検証

```bash
uv run --with-editable=. pyfltr run-for-agent
```

警告ゼロかつテストグリーンで完了。

## fast 判定の計測手順

`{command}-fast` の既定値は実測値で判断する。
最終判断はユーザーが行うため、計測結果のみを提示する。

`fast` はpre-commitフックなどで実行しても作業に支障が出にくい高速ツールを示す。
固定コスト（起動オーバーヘッド）と可変コスト（ファイルあたりの処理時間）の両方に加え、
ツールの重要度や性質も判断材料にする。

### 計測方法

複数プロジェクトで少数ファイルと全ファイルの2パターンを計測し、
`T = a + b * N`（a: 固定コスト、b: ファイルあたりコスト）を推定する。

```bash
# 少数ファイル（対象種別のファイルを1〜2個指定）
uv run --with-editable=. pyfltr run-for-agent <file1> [file2] 2>/dev/null

# 全ファイル（引数なしでリポジトリ全体を対象）
uv run --with-editable=. pyfltr run-for-agent 2>/dev/null
```

JSONL出力の `command` レコードから `command`・`elapsed`・`files` を抽出する。

```bash
... | python3 -c "
import json, sys
for line in sys.stdin:
    r = json.loads(line)
    if r.get('kind') == 'command':
        print(f\"{r['command']:20s} {r['elapsed']:7.2f}s  files={r['files']}\")"
```

推定式: `b = (T_all - T_small) / (N_all - N_small)`、`a = T_small - b * N_small`

`pass-filenames=False` のツール（cargo系・dotnet系・tsc等）はファイル数に関係なく
プロジェクト全体を走査するため、固定コストのみとして扱う。

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
