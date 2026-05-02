# pyfltrのSSOT・参照パス

ドキュメント・実装間で多重管理を避け、SSOTを維持するためのルール。

- 内部リンクは英数アンカーを優先する。
  MkDocs（Material）のslugifyは英数のみを採用してアンカー生成するため、
  日本語アンカーリンク`#見出し日本語`はTOCで解決できずINFO通知のみで`--strict`でも検知されない（手動確認要）。
  markdownlint MD051は見出し原文を見るため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxt `markdown_description`の「対応ツール」節は人手同期（SSOT化しない運用）
- ty記述のSSOTは`docs/guide/index.md`。
  preset非収録の扱いを変更した場合は`README.md`・`mkdocs.yml`内llmstxt・`docs/guide/configuration.md`・`docs/guide/usage.md`を併せて更新する
- サブコマンド一覧のSSOTは`docs/guide/usage.md`。
  サブコマンドを追加・削除した場合は`README.md`の「主なサブコマンド」節と`mkdocs.yml`内llmstxtの「サブコマンド」節を併せて更新する
- `mkdocs.yml`内llmstxt `markdown_description`にはLLMが利用する際に有用な情報のみ記載する（`run-for-agent`サブコマンド・主要オプションなど）。
  LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- 出力形式解決のSSOTは`docs/guide/usage.md`「出力形式の切り替え」節。
  優先順位は`CLI > PYFLTR_OUTPUT_FORMAT > サブコマンド既定値 > AI_AGENT(jsonl) > text`、サブコマンド別許容値は実行系5値・参照系3値・`command-info`2値。
  解決ロジック本体は`pyfltr/cli/output_format.py`の`resolve_output_format`に置く。
  挙動変更時は実装・`docs/guide/usage.md`・`mkdocs.yml`内llmstxtを併せて更新する
- グローバル設定の対象範囲・特殊仕様（archive/cache系のglobal優先）のSSOTは
  `pyfltr/config/config.py`の`ARCHIVE_CONFIG_KEYS` / `CACHE_CONFIG_KEYS` / `GLOBAL_PRIORITY_KEYS`定数。
  対象範囲を拡大する場合は実装・テスト・`docs/guide/configuration.md`を併せて更新する
- mise backend既定値・tool spec組み立て仕様・`mise ls --current`結果に基づくtool spec省略判定のSSOTは
  `pyfltr/command/runner.py`の`_BIN_TOOL_SPEC`および`build_commandline`・関連判定関数。
  変更時は`docs/guide/configuration-tools.md`・`docs/guide/recommended-nonpython.md`・`docs/guide/usage.md`の
  推奨設定例とコマンド表記を併せて更新する
- mise active tools取得結果の構造（`MiseActiveToolsResult`）とステータス語彙7値のSSOTは`pyfltr/command/mise.py`。
  判定／JSONL header露出／`command-info`出力の3経路で同じ結果を共有する設計とする。
  ステータス追加や露出経路を増やすときは`docs/guide/usage.md`（command-info節・JSONLスキーマ節）と
  `docs/development/architecture.md`（mise active tools取得結果の構造化節）も併せて更新する
- モジュールパス参照を含むドキュメントはモジュール移動の際に追従更新が必要。
  主な対象は`CLAUDE.md`・`docs/development/architecture.md`・`.claude/rules/`配下
