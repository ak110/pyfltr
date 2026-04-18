# パート G: retry_command絞り込み＋`--only-failed`フラグ

v3.0.0で追加する `retry_command` ターゲット絞り込み（A案）と `--only-failed` フラグ（B案）の設計判断と仕様。
LLMエージェント向けのJSONL出力の軽量化と、失敗ツール・失敗ファイルを対象とした再実行フローの改善を目的とする。

## 恒常配置の対応先

v3.0.0全体で恒常配置（`docs/features/`・`docs/topics/`）は未整備のため、本パートも開発中配置のみで完結する。
v3リリース完了後に`spec-driven-promote`スキルで恒常配置を整備する際、本ドキュメントは機能ドキュメント（例: `docs/features/再実行支援.md`）へ昇格する候補となる。

## 目的

`retry_command` は現状ツール単位で対象ファイルを全列挙するため、JSONL出力がファイル数に比例して肥大化する（パートF開発中に30KB超を観測）。
LLMエージェントが失敗ツールだけを再実行する用途では、全ファイル指定は冗長かつコンテキスト消費の面で不利となる。

本パートで以下の2系統を導入し、LLMエージェント向けのJSONL出力と再実行フローを改善する。

- A案（`retry_command` 絞り込み）: `retry_command` のターゲットを当該ツールで失敗したファイルのみに絞る（常時有効）
- B案（`--only-failed` フラグ）: 直前runから失敗ツール・失敗ファイルを抽出し、ツール別に失敗ファイル集合のみを個別対象として再実行する

## A案: retry_command絞り込み

`retry_command` のターゲット位置引数を、当該ツールで失敗したファイル（`CommandResult.errors` から抽出）のみに絞る。

主な仕様:

- ターゲット絞り込みは常時有効（既存挙動を上書き）。ツール単位1件の `retry_command` 出力は維持する
- 新ヘルパー `_populate_retry_command()` を追加し、`_attach_retry_command()` クロージャはそれに委譲する。`_populate_retry_command()` 内で `_filter_failed_files()` を呼び、絞り込み後の `target_files` を `_build_retry_command()` へ渡す。`_build_retry_command()` のインターフェースは変えない
- 失敗ファイルの抽出元は `CommandResult.errors` の `ErrorLocation.file` 集合。`CommandResult.target_files` との交差を並び順を保って返す（`_filter_failed_files()` ヘルパーで実装）
- 失敗ファイル集合が空の場合（`ErrorLocation.file` が取得できない・全体失敗のみ等）は、`retry_command` のターゲット位置引数を空にする（省略しない）
- キャッシュ復元結果（`cached=True`）では `retry_command` を埋めない（再実行不要、`retry_command=None` のまま）

## B案: `--only-failed` フラグ

直前runから失敗ツールと失敗ファイルを抽出し、ツール別に失敗ファイル集合のみを対象として再実行する。

主な仕様:

- 直前run特定は `ArchiveStore.list_runs(limit=1)` の先頭を採用する
- 失敗ツール判定は `read_tool_meta(run_id, tool).status == "failed"` の読み取りで行う
- 失敗ファイル抽出は `read_tool_diagnostics(run_id, tool)` の各エントリの `file` フィールド集合を使う
- 絞り込み結果はツール別の失敗ファイル集合 `dict[str, list[pathlib.Path] | None]` として保持する
    - 値が `None`: 直前runに診断ファイルが無く失敗ファイルを特定できない（pytest等のpass-filenames=Falseで全体失敗のみ）→ 通常の `all_files` 相当で再実行
    - 値が `[]`: 診断から失敗ファイルを特定できたが `targets` との交差が空 → 当該ツールを再実行対象から除外
- 絞り込み結果は `run_pipeline` → `cli`/`ui` → `execute_command` の経路で各ツールに自分の失敗ファイル集合のみを渡す（ツール間で対象ファイルを共有しない）
- 直前runが存在しない、または失敗ツールが無い場合はメッセージを出して成功終了（rc=0）
- 早期終了判定: `commands` が空、または全ての失敗ツールで値が `[]`（交差空）となった場合に早期終了。値が `None` のツールは「再実行対象あり」として扱い早期終了しない
- 位置引数 `targets` との併用: 直前runの失敗ファイル集合と `targets` を交差させる

## スコープ

| 項目 | 内容 |
| --- | --- |
| 対象モジュール | `pyfltr/main.py`（A案絞り込みヘルパー追加・B案フラグ追加・B案フィルター追加） |
| 対象モジュール | `pyfltr/command.py`（`execute_command()` に `only_failed_files` 引数追加） |
| 対象モジュール | `pyfltr/cli.py` / `pyfltr/ui.py`（`only_failed_targets` 引数追加） |
| 既存基盤（読み取り） | `pyfltr/archive.py` の `list_runs` / `read_tool_meta` / `read_tool_diagnostics` |
| 既存基盤（A案書き込み） | `pyfltr/command.py` の `CommandResult.errors` / `target_files` / `retry_command` |
| CLI追加 | `--only-failed` フラグ（`run-for-agent` および `run` サブコマンド共通） |
| 設定キー追加 | なし |
| 他パートとの関係 | パートB（アーカイブ読み取り経路）・パートC（`retry_command`埋め込み）・パートD（`--fail-fast`の先例）に依存 |
| スコープ外 | C案（`retry_command` をJSONLから除去して `tool.json` 経由参照にする案）・`latest` エイリアス・`--from-run` オプション |

## 受け入れ基準

- `retry_command` のターゲットが、当該ツールで失敗したファイルのみに絞られた状態で出力される
- 失敗ファイルが特定できない場合（全体失敗のみ等）は、ターゲット位置引数が空の `retry_command` が出力される
- `cached=True` の結果では `retry_command` が `null` のまま出力される
- `--only-failed` 指定で直前runの失敗ツール・失敗ファイルのみを対象とした再実行が行われる
- `--only-failed` と `targets` 位置引数を併用した場合、失敗ファイル集合と `targets` の交差を対象とした再実行が行われる
- `--only-failed` 指定で直前runが存在しない・失敗ツールが無い場合はメッセージを出してrc=0で終了する
- 診断なしの失敗ツール（pytest等）は `--only-failed` 時も通常の `all_files` 相当で再実行される

## 主要設計判断

### A案の失敗ファイル抽出を `CommandResult.errors` のみから行う

既存データを再利用する。`ErrorLocation.file` の集合と `CommandResult.target_files` を交差させ、`target_files` の並び順を保つ。
パス比較は文字列化した相対パス同士で行う（`ErrorLocation.file` と `expand_all_files` 由来の `target_files` はどちらもcwd基準の相対パスが前提）。

### B案の絞り込みを `run_pipeline` 内の早い段階に挿入する

`expand_all_files` 直後・archive/cache初期化前に挿入する。
今回のrunのrun_id/cache_storeに影響させないためである。

### `None` と `[]` を明確に分離する

ツール別ターゲット辞書の値を `list[pathlib.Path] | None` とし、意味を明確に分離する。

- `None`: 診断なし（pass-filenames=Falseのツール等）→ 既定の `all_files` 相当で実行するフォールバック
- `[]`: 診断から失敗ファイルを特定できたが `targets` との交差が空 → 当該ツールを除外

この分離により、「診断なしで全体再実行が必要なケース」と「絞り込み後に再実行対象がないケース」を誤って同一扱いすることを防ぐ。

### `execute_command()` の `only_failed_files` 引数で上書き経路を吸収する

`execute_command()` の入り口で `only_failed_files: list[pathlib.Path] | None` を受け取る。
`None` の場合は既定の `files` 引数で実行し、`list` の場合はその集合を対象ファイルとして使う。
その後の `target_extensions` フィルタや `pass_filenames=False` の分岐は既存通り適用する。
`[]` が渡ってきた場合の扱いは `run_pipeline` 側で `commands` から除外する方針とし、`execute_command` まで到達しない前提（念のため空リストが渡っても正常にskip扱いとする防御コードを入れる）。

## 却下した代替案

- **A案・B案・C案のフルセット（C案を含む）**: スコープ拡大のため却下。C案（`retry_command` をJSONLから除去して `tool.json` 経由参照にする案）は本パートでは対象外
- **A案のみ（B案なし）**: LLM用途では `retry_command` 出力の軽量化だけでは不十分なため却下
- **`--only-failed` で直前runが無い時にエラー終了する案**: 反復的CI運用で不便なため却下。rc=0で成功終了する
- **`--only-failed` に `targets` 指定を禁止する案**: 意図の複合指定（ディレクトリ絞り込み＋失敗絞り込み）を妨げるため却下
- **`--only-failed` を「失敗ツール集合×失敗ファイル和集合」の直積粒度で実装する案**: 直前runでは成功していたツール・ファイルのペアまで再実行してしまうため却下。ツール別に失敗ファイル集合を個別に渡す
- **A案で絞り込み結果が空のときに `retry_command` を省略する案**: pytestのように全体指定で意味を持つツールがあるため、省略ではなく空 `target_files` で出力する方針を採る

## 関連ファイル

実装配置:

- `pyfltr/main.py`: `_filter_failed_files()` と `_populate_retry_command()` 追加（A案）
- `pyfltr/main.py`: `--only-failed` フラグと `_apply_only_failed_filter()` を追加し、`run_pipeline()` の `expand_all_files` 直後に挿入（B案）
- `pyfltr/command.py`: `execute_command()` に `only_failed_files` 引数追加
- `pyfltr/cli.py` / `pyfltr/ui.py`: `only_failed_targets` 引数追加
- `tests/main_test.py`: `_filter_failed_files` / `_populate_retry_command` / `_apply_only_failed_filter` / `--only-failed` の各テスト追加
- `tests/command_test.py`: `execute_command()` の `only_failed_files` 引数テスト追加

ドキュメント:

- `docs/guide/usage.md`: `--only-failed` オプションの説明・`retry_command` が失敗ファイルのみに絞られる挙動の追記
- `docs/v3/index.md`: パートG（`retry_command` 絞り込み・`--only-failed`）の概要追加
- `docs/v3/作業ステータス.md`: 進捗表と本内訳を更新
- `mkdocs.yml`: navへの本ドキュメント追加・llmstxt `markdown_description` への `--only-failed` 追記
- `CHANGELOG.md`: `[Unreleased] ### 追加` への `--only-failed` フラグと `retry_command` 絞り込みの追記
- `CLAUDE.md`: 必要に応じて `--only-failed` と `retry_command` 絞り込み挙動の案内追記

## 関連ドキュメント

- [JSONL出力拡張](JSONL出力拡張.md): `retry_command` フィールドのスキーマ定義（パートC）
- [実行アーカイブ](実行アーカイブ.md): B案の直前run参照に使う `ArchiveStore` 読み取り経路（パートB）
- [パイプライン機能拡張](パイプライン機能拡張.md): `--fail-fast` / `--no-cache` の先例（パートD）
