# pyfltrの出力形式とlogger・LLM出力スキーマ

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。
実装を変更する際はこの設計判断を崩さないこと。

format別のstream/level切替の詳細は[docs/development/architecture.md](../../docs/development/architecture.md#logger)を参照。

- root（system logger）: 常にstderr。抑止しない。設定エラー・アーカイブ初期化失敗などを送出する
- `pyfltr.textout`: 人間向けテキスト出力。`pyfltr.cli.output_format.configure_text_output(stream, *, level)`で切り替える
- `pyfltr.structured`: JSONL / SARIF / Code Qualityの構造化出力。`pyfltr.cli.output_format.configure_structured_output(dest)`で切り替える

stdout占有は`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ発生する。

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
  `resolution_failed`のような付加情報のみ0件で省略する
- 個別ルールの`command.hints`とパイプライン全体の`summary.guidance`は粒度・性質が異なるため命名を分ける
- `command.hints`・`summary.guidance`はLLM入力前提のため英語で記述する。
  トークン効率と汎用性を優先し、「全文章は日本語」方針より優先する例外として扱う
