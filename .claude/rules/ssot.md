---
paths:
  - "pyfltr/config/config.py"
  - "pyfltr/cli/output_format.py"
  - "pyfltr/command/runner.py"
  - "pyfltr/command/mise.py"
  - "docs/guide/*.md"
  - "docs/development/*.md"
  - "mkdocs.yml"
  - "README.md"
  - "CLAUDE.md"
  - ".claude/rules/*.md"
---

# pyfltrのSSOT・参照パス

- 内部リンクは英数アンカーを優先する。
  MkDocs（Material）のslugifyは英数のみを採用してアンカー生成するため、
  日本語アンカーリンク`#見出し日本語`はTOCで解決できずINFO通知のみで`--strict`でも検知されない（手動確認要）。
  markdownlint MD051は見出し原文を参照するため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- MkDocsの`docs_dir`（`docs/`）外のファイル（`.claude/rules/`配下など）への内部リンクは禁止する。
  `docs_dir`外のファイルはサイトに含まれず`--strict`でビルドが失敗するため。
  内部規約・運用ファイルへの参照は本文中での言及にとどめる
- `docs/guide/index.md`の対応ツール一覧と`mkdocs.yml`内llmstxtの`markdown_description`「対応ツール」節は
  人手同期（SSOT化しない運用）。
  ただし`markdown_description`本文については`tests/llmstxt_test.py`が
 「全サブコマンド名・全ビルトインコマンド名を含むこと」を機械検証する。
  整理時に圧縮しすぎると当該テストが失敗するため、これらの名前は漏らさず記載する
- ty記述のSSOTは`docs/guide/index.md`。
  preset非収録の扱いを変更した場合は`README.md`・`mkdocs.yml`内llmstxt・`docs/guide/configuration.md`・
  `docs/guide/usage.md`を併せて更新する
- サブコマンド一覧のSSOTは`docs/guide/usage.md`。
  サブコマンドを追加・削除した場合は`README.md`の「使い方」節と`mkdocs.yml`内llmstxtの「サブコマンド」節を併せて更新する
- `mkdocs.yml`内llmstxtの`markdown_description`にはLLMが利用する際に有用な情報のみ記載する
 （`run-for-agent`サブコマンド・主要オプションなど）。LLMにとって不要な情報はdocs側をSSOTとし、多重管理を避ける
- 出力形式解決のSSOTは`docs/guide/usage.md`「出力形式の切り替え」節。
  優先順位は`CLI > PYFLTR_OUTPUT_FORMAT > サブコマンド既定値 > AI_AGENT(jsonl) > text`。
  サブコマンド別許容値は実行系5値・参照系3値・grep / replace 3値・`command-info`2値。
  解決ロジック本体は`pyfltr/cli/output_format.py`の`resolve_output_format`に置く。
  挙動変更時は実装・`docs/guide/usage.md`・`mkdocs.yml`内llmstxtを併せて更新する
- グローバル設定の対象範囲・特殊仕様（archive/cache系のglobal優先）のSSOTは
  `pyfltr/config/config.py`の`ARCHIVE_CONFIG_KEYS` / `CACHE_CONFIG_KEYS` / `GLOBAL_PRIORITY_KEYS`定数。
  対象範囲を拡大する場合は実装・テスト・`docs/guide/configuration.md`を併せて更新する
- 新規`{prefix}-{key}`系設定キー（`{prefix}-max-*`等）を`pyfltr/config/config.py`の
  `DEFAULT_CONFIG`へ追加する場合、`docs/guide/configuration.md`の「設定項目一覧」節
 （`#config-keys`）も併せて更新する。コードと参照ドキュメントが乖離しやすい
- mise backend既定値・tool spec組み立て仕様・`mise ls --current`結果に基づくtool spec省略判定のSSOTは
  `pyfltr/command/runner.py`の`_BIN_TOOL_SPEC`および`build_commandline`・関連判定関数。
  変更時は`docs/guide/configuration-tools.md`・`docs/guide/recommended-nonpython.md`・`docs/guide/usage.md`の
  推奨設定例とコマンド表記を併せて更新する
- mise active tools取得結果の構造（`MiseActiveToolsResult`）とステータス語彙7値のSSOTは`pyfltr/command/mise.py`。
  判定／JSONL header露出／`command-info`出力の3経路で同じ結果を共有する設計とする。
  ステータス追加や露出経路を増やすときは`docs/guide/usage.md`の「command-info」節と
  `docs/development/architecture.md`（mise active tools取得結果の構造化節）も併せて更新する
- モジュールパス参照を含むドキュメントはモジュール移動の際に追従更新が必要。
  主な対象は`CLAUDE.md`・`docs/development/architecture.md`・`.claude/rules/`配下
- `.claude/rules/`配下のルールファイルが`paths` frontmatterで連動更新先
 （実装ファイル・テストファイル等）を列挙する場合、ルール改訂・連動先リネーム・新規追加の各タイミングで
  pathsとリポジトリ実体の整合を計画段階で検証する。
  実在しないパスを残すと条件付きロードのトリガーとして機能せず、想定したコンテキスト投入が動作しない
- grep/replace機能の設計判断・undo方式・MCP既定値違いのSSOTは`pyfltr/.claude/rules/grep-replace.md`。
  新ツール固有の機能拡張（オプション追加・JSONLレコード追加・MCPツール追加）はそちらと併せて見直す
- `.claude/rules/`配下から実装挙動の詳細をコード側docstringへ移譲する場合、
  ルール側は方針（設計判断・採用しない選択肢の理由など）のみ残し、挙動手順の再掲は避ける。
  同じ内容がルール側とコード側に並ぶ二重管理状態は更新ずれの温床となるため。
  圧縮時には「実装詳細（手順記述）」と「設計判断・将来検討メモ（対症療法である旨・撤去判断の前提など）」を区別し、
  後者は削除せずコード側docstringへ確実に移譲する
