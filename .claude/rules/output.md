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

## logger役割分担

pyfltrは3系統のlogger（root system / `pyfltr.textout` / `pyfltr.structured`）を使い分ける。
役割と切替手段は`pyfltr/cli/output_format.py`の`configure_text_output` /
`configure_structured_output` のdocstringに集約する。

## JSONL公開ヘルパー方針

JSONL出力経路は`pyfltr/output/jsonl.py`の公開ヘルパー（`emit_record` / `emit_records`）経由に統一する。
モジュール外から`_write_lock`・`_emit_structured`を直接参照しない（`pylint: disable=protected-access`の常態化を避けるため）。

## LLM出力スキーマ

- JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）。
  詳細仕様は実装docstringに集約し、`docs/guide/usage.md`・`mkdocs.yml`内llmstxtに
  個別フィールドの詳細仕様を書かない（LLMはコード読みで補完する前提）
- JSONL出力の`command.hints`は「対応する指摘やステータスが実際に該当するときのみ付与する」方針。
  指摘0件の実行で固定的なhintが残るとLLM入力のトークンを浪費するため、
  `aggregate_diagnostics`由来のhintは指摘ある時のみ集約し、
  ツール固有のhint（`messages[].col`等）も付与条件に当該指摘・状態の存在を含める。
  per-tool `{command}-hints`は指摘1件以上のときに限り`user.<n>`連番キーで追加する。
  複数の関連フィールドに同じ説明文が及ぶ場合は代表キー1つに統合し、キー数・文言数を抑える
- 個別ルールの`command.hints`とパイプライン全体の`summary.guidance`は粒度・性質が異なるため命名を分ける
- `command.hints`・`summary.guidance`はLLM入力前提のため英語で記述する。
  トークン効率と汎用性を優先し、「全文章は日本語」方針より優先する例外として扱う
- JSONL `command.status` 語彙のSSOTは`pyfltr/command/core_.py`の`CommandResult.status`プロパティのdocstring。
  新規status値追加時は当該docstringと判定分岐を併せて更新する
- `summary`レコードのフィールド順序仕様のSSOTは`pyfltr/output/jsonl.py`の`_build_summary_record`のdocstring
- JSONL commandレコードの`effective_runner`・`runner_source`・`runner_fallback`は
 「期待した経路と実際の経路が乖離した場合」に限り出力する（fallback検出用）。
  通常経路は3フィールドとも省略してLLM入力のトークン消費を抑える方針。
  通常時の解決状況の確認は`pyfltr command-info`サブコマンドの責務とする
- 段階出力イベント（`status:"running"`）の発火条件・発火時挙動・最終レコード後続保証の設計は
  `pyfltr/cli/pipeline.py`の`HeartbeatMonitor`のdocstringを参照する。
  buffering型formatter（SARIF・Code Quality）でheartbeat由来のrunningイベントを混入させない理由は
  `run_pipeline`のdocstringに集約する
