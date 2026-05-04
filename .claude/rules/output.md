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
- JSONL出力経路は`pyfltr/output/jsonl.py`の`_emit_structured`ヘルパー1点に集中させる。
  当該ヘルパーは書き込みと同時に最終出力時刻（`_last_jsonl_output_time`）を更新し、
  heartbeat監視スレッドが「最後のJSONL出力からの無音時間」を判定できる状態を保つ。
  新規イベント種別を追加する際は当該ヘルパー経由で出力し、
  時刻記録が自動追跡される配置を維持する

## LLM出力スキーマ

- JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）
- JSONL出力フィールドは自己説明性を優先する。
  LLM向けに極力読めば分かる構造を維持しており変更頻度も高いため、ドキュメントには詳細仕様を記載せず
  コード読みでの補完を前提とする。
  `docs/guide/usage.md`・`mkdocs.yml`内llmstxtにJSONLレコードや個別フィールドの詳細仕様を書かない
- JSONL出力の`command.hints`は「対応する指摘やステータスが実際に該当するときのみ付与する」方針とする。
  指摘0件の実行で固定的なhintが残るとLLM入力のトークンを浪費するため、
  `aggregate_diagnostics`由来のhintは指摘ある時のみ集約し、
  ツール固有のhint（`messages[].col`等）も付与条件に当該指摘・状態の存在を含める
- 複数の関連フィールド（例:`messages[].col`と`messages[].end_col`）に同じ説明文が及ぶ場合は、
  代表キー1つに統合した1つのhintで両方をまとめて説明する。
  類似文言の重複はLLM入力のトークンを浪費するため、付与時はキーを1個・文言を1個に集約する
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
- per-tool `{command}-hints`はLLM向けの補足文言を利用者側から付与する設定。
  指摘1件以上のときに限り、JSONL `command.hints` の `user.<n>` 連番キーで追加する。
  指摘0件の実行で固定的なhintを残してLLM入力のトークンを浪費しないよう、
  `messages[].col`等のツール固有hintと同じく「対応する指摘・状態が該当するときのみ付与する」方針に揃える
- 段階出力イベント（`status:"running"`）は実行中の進行状況を可視化する目的に限り発行する。
  発行条件は「最後のJSONL出力から一定時間（既定30秒）経過した場合」（パイプライン全体heartbeat連動）に限り、
  subprocess側のstdout出力の有無は判定材料にしない。
  LLMから観測できるのはpyfltr側のJSONL出力のみであり、子プロセスの静粛さは観測対象外のため。
  発火時は実行中の全コマンドそれぞれにrunningイベントを発行し、
  複数コマンドが同時にハングしている場合も各コマンドの状態が識別できるようにする。
  完了時の最終レコード（`status:"failed"`/`"succeeded"`等）が必ず後続することを保証する設計とする
