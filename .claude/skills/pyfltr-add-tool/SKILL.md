---
name: pyfltr-add-tool
description: >
  pyfltr に新しい formatter / linter / tester を追加する際の定型手順チェックリスト。
  tools.py / config/model.py / command/dispatcher.py / parsing/tools.py /
  docs/guide/index.md / tests / .github/workflows などを一貫して更新する。
---

# pyfltr 新ツール追加チェックリスト

## 触るべきファイル

用途が近い既存ツールを1つ雛形として選ぶ。既存の実行・解析方式で扱える通常の追加では、
実装の登録変更は`BUILTIN_COMMANDS`の1件で完結する。テスト・導入設定・利用者文書は以下に従って追随する。

- `pyfltr/tools.py`: `BUILTIN_COMMANDS`の1件の`CommandInfo`へ登録する。
  既定値、言語カテゴリ、runnerと標準版、専用実行種別、解析・構造化出力・ルールURLを同じ定義に置く。
  設定・カテゴリ・エイリアス・解析登録はここから導出され、別の表へ転記しない
- 新しい実行方式が必要な場合は`docs/development/architecture.md`の「専用実行方式の追加」に従い、
  `pyfltr/command/dispatcher.py`と担当実行モジュールを更新する
- `pyfltr/parsing/tools.py`: 既存の解析方式で扱えない場合にツール固有パーサーを実装し、
  `tools.py`の`CommandInfo.parser`へ登録する。regexだけの場合は定義内の`diagnostic_pattern`へ置く
- `tests/`: 実装と同じ相対位置の`config/`・`command/`・`parsing/`に対応するテストを追加
- `tests/integration/smoke_test.py`: 新ツールのsmoke testケースを追加
- `tests/smoke_data/<tool>_workspace/`: 最小の実行対象と設定を含むsmoke test用workspace一式を追加
- `.github/workflows/ci.yaml`: CIで新ツールを利用できるようインストール手順を追加
- `docs/guide/index.md`:「対応ツール」一覧へ追記（`README.md`には書かない）
- `mkdocs.yml`: `plugins.llmstxt.markdown_description` の対応ツール一覧へ新ツール名を追記する
  - ベタ書きで自動同期されないため、追記の抜けは
    `tests/integration/llmstxt_test.py::test_llmstxt_contains_all_builtin_commands` の失敗につながる
- `docker/Dockerfile`: 対応ツールは公式Dockerイメージ（`ghcr.io/ak110/pyfltr`）に事前同梱する。
  導入方法の区分は冒頭コメントの「同梱ツール一覧」を参照する。
  Rust / .NETツールチェイン依存は対象外

## 気付きにくい注意点

- `aliases` の `format` / `lint` / `test` への登録を忘れると、`--commands=lint` 等で対象から漏れる
- bin-runner対応ツール（miseバックエンド経由のネイティブバイナリ）は、通常の4キーに加えて
  `-version` キーを必須とし、`-path` の既定値は空文字列にする
- カスタム関数パーサーは`CommandInfo.parser`へ登録しないと有効化されない
- 終了位置を出力するツールでは、終了行が範囲の最終行を指すか範囲末尾の次の位置を指すかを公式仕様で確認する
  - `ErrorLocation.end_line`は最終行を含む値として扱う
  - `parsing/common.py`の`to_inclusive_end_position`へ開始行・終了行・終了列を渡し、返る組をそのまま格納する
- 依存追加は `uv add` を使う（`uv.lock` の直接編集はPreToolUse hookでブロックされる）
- 外部パス対応分類を決定する。
  対象は`allows_external_paths`・`config_arg_template`・`config_inject_candidates`とする。
  分類方針の詳細は `.claude/rules/targets.md` を参照する
  - 起点cwd外のファイルを渡すと想定外の動作になるツールへ`allows_external_paths=False`を指定する。
    `gitleaks`等の全体走査型、`pytest`等のtester、pre-commit・prekが該当する
  - 起点cwd直下の設定ファイルを外部パス指定時にも適用したいツールは
    `config_arg_template=["--config", "{path}"]` と `config_inject_candidates=[...]` を指定する
  - 上記以外は既定値（素通し）のままとする
  - `allows_external_paths=False` を新規指定した場合は、
    `tests/integration/external_paths_test.py::test_external_path_filtered_with_warning` の対象一覧へ
    対象のツール名と境界確認用の対象パターンを1件追加する
- `error_parser` の正規表現には英単語を完全な形で書く
  - 単語の一部だけの文字列はtyposが既知の誤記と判定しpre-commitがブロックする
  - 前方一致が必要でも `(?:vulnerability|vulnerabilities)` のように単語を完結させる
- 新ツールの並び順は `.claude/rules/order.md` の「並び順を揃える箇所」「領域別の末尾追加方針」に従う
  - 新しい領域を設ける場合はorder.mdへ配置順も追記する

## 既存ツールと専用の実行処理を共有する新ツールの追加

`pre-commit`と`prek`のように、既存ツールと同一の専用の実行処理を共有する新ツールを扱う。
専用の実行処理は`command/dispatcher.py`の登録表と`command/precommit.py`等の専用実行モジュールを指す。
この形の新ツールを追加する場合は以下の正規フローを適用する。

- 専用実行モジュールの出力メッセージと設定キー参照をツール名でパラメーター化する
- `CommandInfo.execution_kind`へ既存の種別を指定する。新しい実行方式が必要な場合だけ
  実行要求型とdispatcherの種別登録を追加する
- `{ツール名}-`接頭辞の設定キー群一式（`-path`・`-args`・`-fast`等）を新ツール用に複製する。
  新ツールが既存ツールと同じ設定値を参照する仕様の場合は複製しない
- 専用テストをツール名でパラメーター化する。
  入力・期待値・実行処理が異なり、単一テストの分岐が必要になる場合はテストを複製する
- 同時有効化で同一処理を重複実行する場合は設定警告を発行する。
  実行対象または設定ファイルが異なり競合しない場合は警告を追加しない

## 検証

```bash
uv run pyfltr run
```

警告ゼロかつテスト全件成功で完了とする。

## fast 判定の計測手順

`{command}-fast` の既定値は計測値で判断する。最終判断はユーザーが行うため、計測結果のみを提示する。

`fast` はpre-commitフックなどで実行しても作業に支障が出にくい高速ツールを示す。
固定コスト（起動オーバーヘッド）と可変コスト（ファイルあたりの処理時間）の両方に加え、
ツールの重要度や性質も判断材料にする。

### 計測方法

複数プロジェクトで少数ファイルと全ファイルの2パターンを計測し、
`T = a + b * N`（a: 固定コスト、b: ファイルあたりコスト）を推定する。

```bash
# 少数ファイル（対象種別のファイルを1〜2個指定）
uv run pyfltr run --output-format=jsonl <file1> [file2] 2>/dev/null

# 全ファイル（引数なしでリポジトリ全体を対象）
uv run pyfltr run --output-format=jsonl 2>/dev/null
```

JSONL出力の `command` レコードの `elapsed` と `files` から `a`・`b` を算出する。
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
