---
paths:
  - "pyfltr/output/**"
  - "pyfltr/cli/output_format.py"
  - "pyfltr/cli/pipeline.py"
  - "pyfltr/command/core_.py"
  - "pyfltr/command/dispatcher.py"
  - "tests/llm_output_test.py"
  - "tests/output_format_test.py"
  - "tests/pipeline_heartbeat_test.py"
  - "tests/sarif_output_test.py"
  - "tests/code_quality_test.py"
  - "tests/llmstxt_test.py"
  - "docs/guide/usage.md"
  - "docs/development/architecture.md"
  - "mkdocs.yml"
---

# pyfltrの出力形式とlogger・LLM出力スキーマ

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。
実装を変更する際はこの設計判断を崩さないこと。

format別のstream/level切替の詳細は[docs/development/architecture.md](../../docs/development/architecture.md#logger)を参照。

- root（system logger）: 常にstderr。抑止しない。
  設定エラー・アーカイブ初期化失敗などを送出する
- `pyfltr.textout`: 人間向けテキスト出力。
  `pyfltr.cli.output_format.configure_text_output(stream, *, level)`で切り替える
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。
  `pyfltr.cli.output_format.configure_structured_output(dest)`で切り替える

stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

- 新規formatterまたは構造化出力イベントを追加する際は、出力先formatterの種別を確認する。
  SARIFとCode Qualityは`on_finish`で単一JSONドキュメントを一括出力するbuffering型である。
  パイプライン途中で`{"kind":"running"}`等のJSONLレコードを混入させると、
  最終的な出力（SARIF 2.1.0オブジェクト・Code Climate JSON配列）が破損する。
  heartbeat由来のrunningイベントが`jsonl`形式に限定して起動される根拠もここにある
 （実装上の対処は`pyfltr/cli/pipeline.py`の`run_pipeline`参照）
- JSONL出力経路は`pyfltr/output/jsonl.py`の公開ヘルパー
 （`emit_record` / `emit_records`）経由に統一する。
  両ヘルパーは内部で`_write_lock`取得と`_emit_structured`呼び出しを完結する。
  本モジュール外の呼び出し側は`_write_lock`／`_emit_structured`等のprivate属性を直接参照しない。
  これは`pylint: disable=protected-access`の常態化を避けるためである。
  `_emit_structured`は最終出力時刻（`_last_jsonl_output_time`）を更新し、
  heartbeat監視スレッドが「最後のJSONL出力からの無音時間」を判定できる状態を保つ。
  新規イベント種別を追加する際も公開ヘルパー経由で出力し、時刻記録が自動追跡される配置を維持する

## LLM出力スキーマ

- JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）
- JSONL出力フィールドは自己説明性を優先する。
  LLM向けに極力読めば分かる構造を維持しており変更頻度も高いため、ドキュメントには詳細仕様を記載せず
  コード読みでの補完を前提とする。
  `docs/guide/usage.md`・`mkdocs.yml`内llmstxtにJSONLレコードや個別フィールドの詳細仕様を書かない
- JSONL出力の`command.hints`は「対応する指摘やステータスが実際に該当するときのみ付与する」方針とする。
  指摘0件の実行で固定的なhintが残るとLLM入力のトークンを浪費するため、
  `aggregate_diagnostics`由来のhintは指摘ある時のみ集約し、
  ツール固有のhint（`messages[].col`等）も付与条件に当該指摘・状態の存在を含める。
  per-tool `{command}-hints`（利用者側から付与する補足文言）も同方針に揃え、
  指摘1件以上のときに限り`user.<n>`連番キーで追加する。
  複数の関連フィールド（例:`messages[].col`と`messages[].end_col`）に同じ説明文が及ぶ場合は、
  代表キー1つに統合した1つのhintで両方をまとめて説明し、キーを1個・文言を1個に集約する
- `summary.commands_summary`統計は`failed`等の判定に必要な項目を常時出力し、
  `resolution_failed`のような付加情報のみ0件で省略する。
  `warning`は`needs_action`配下で`failed`と並列に常時出力する
 （0件であることが警告無し判定に直結するため）
- 個別ルールの`command.hints`とパイプライン全体の`summary.guidance`は粒度・性質が異なるため命名を分ける
- `command.hints`・`summary.guidance`はLLM入力前提のため英語で記述する。
  トークン効率と汎用性を優先し、「全文章は日本語」方針より優先する例外として扱う
- JSONL `command.status`の語彙は次の通り。`status`は`resolution_failed`・`returncode`・
  `has_error`・`command_type`・`timeout_exceeded`・`severity`から導出する計算プロパティのため、
  新規追加・名称変更時は本一覧と`pyfltr/command/core_.py`の`status`プロパティの
  判定分岐を併せて更新する。
  - `succeeded`: 通常成功（returncode==0）
  - `formatted`: formatterがファイルを書き換えた成功（再実行不要）
  - `skipped`: 対象ファイル0件等で起動しなかった
  - `failed`: 通常の失敗。`severity`が既定値`"error"`のとき採用する
  - `warning`: per-tool `{command}-severity = "warning"` 設定下での失敗格下げ。
    パイプライン全体exit codeに影響しない。`commands_summary.needs_action.warning`へ集計し、
    `summary.guidance`のfailure系文言は出力しない。
    `resolution_failed` / `timeout_exceeded`はツール起動自体の異常で警告扱いに馴染まないため、
    severityの影響を受けず`failed`/`resolution_failed`のままとなる
  - `resolution_failed`: ツール起動コマンドの解決に失敗した
  - `running`: heartbeat由来の実行中レコード
- JSONL commandレコードの`effective_runner`・`runner_source`・`runner_fallback`は
 「期待した経路と実際の経路が乖離した場合」に限り出力する（fallback検出用）。
  通常経路（runnerで指定したカテゴリ・直接値の通り解決した場合、`{command}-path`指定でdirect固定の場合等）は省略する。
  LLM入力のトークン消費を抑えるため、通常時は3フィールドとも出力しない方針とする。
  fallback発生時は`runner_fallback`に退行経路を記録する（例:`"uv->direct"`）。
  通常時の解決状況の確認は`pyfltr command-info`サブコマンドの責務とする
- `summary`レコードのフィールド順序は「結論→集計→指摘総数→ガイダンス→ファイル情報」の流れに揃える。
  必須キーは`kind`→`exit`→`commands_summary`→`diagnostics`の順とする。
  条件付きキーは`guidance`→`applied_fixes`→`fully_excluded_files`→`missing_targets`の順とする。
  コマンド単位の集計（statusカテゴリ別件数およびコマンド総数`total`）は`commands_summary`配下に集約し、
  `total`は`no_issues`・`needs_action`の末尾に置く。
  `diagnostics`はコマンド単位の集計ではなく指摘総件数のため`commands_summary`の外に置く
- 段階出力イベント（`status:"running"`）は実行中の進行状況を可視化する目的に限り発行する。
  発行条件は「最後のJSONL出力から一定時間（既定30秒）経過した場合」（パイプライン全体heartbeat連動）に限り、
  subprocess側のstdout出力の有無は判定材料にしない。
  LLMから観測できるのはpyfltr側のJSONL出力のみであり、子プロセスの静粛さは観測対象外のため。
  発火時は実行中の全コマンドそれぞれにrunningイベントを発行し、
  複数コマンドが同時にハングしている場合も各コマンドの状態が識別できるようにする。
  完了時の最終レコード（`status:"failed"`/`"succeeded"`等）が必ず後続することを保証する設計とする
