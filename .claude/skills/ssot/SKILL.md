---
name: ssot
description: >
  pyfltrのSSOT・参照パスの方針。
  内部リンクのアンカー方針・MkDocsのdocs_dir外参照禁止・規約適用ルール・モジュールパス参照追従・
  SSOT俯瞰（連動更新先の一覧）・機能追加時の文書露出判断を集約する。
  pyfltr/config/config.py・pyfltr/cli/output_format.py・pyfltr/command/runner.py・
  pyfltr/command/mise.py・pyfltr/state/archive.py・pyfltr/state/cache.py・
  docs/guide/*.md・docs/development/*.md・mkdocs.yml・README.md・CLAUDE.md・
  .claude/skills/*/SKILL.md・.claude/rules/*.md を編集する際に使用する。
---

# pyfltrのSSOT・参照パス

## 内部リンクと参照範囲の方針

- 内部リンクは英数アンカーを優先する。
  MkDocs（Material）のslugifyは英数のみを採用してアンカー生成するため、
  日本語アンカーリンク`#見出し日本語`はTOCで解決できずINFO通知のみで`--strict`でも検知されない（手動確認要）。
  markdownlint MD051は見出し原文を参照するため、`{#id}`記法で明示併設する（例:「### jsonl形式の使い方 {#jsonl}」）
- MkDocsの`docs_dir`（`docs/`）外のファイル（`.claude/skills/`配下や`.claude/rules/`配下など）への内部リンクは禁止する。
  `docs_dir`外のファイルはサイトに含まれず`--strict`でビルドが失敗するため。
  内部規約・運用ファイルへの参照は本文中での言及にとどめる

## 規約適用時の運用ルール

- `.claude/skills/`配下・`.claude/rules/`配下から実装挙動の詳細をコード側docstringへ移譲する場合、
  スキル・ルール側は方針（設計判断・採用しない選択肢の理由など）のみ残し、挙動手順の再掲は避ける。
  圧縮時には「実装詳細（手順記述）」と「設計判断・将来検討メモ（対症療法である旨・撤去判断の前提など）」を区別し、
  後者は削除せずコード側docstringへ確実に移譲する
- `.claude/skills/`配下のスキルが`description`の対象範囲を列挙する場合、および
  `.claude/rules/`配下のルールファイルが`paths` frontmatterで連動更新先を列挙する場合、
  スキル・ルール改訂・連動先リネーム・新規追加の各タイミングでスキル/ルール側の記述とリポジトリ実体の整合を
  計画段階で検証する。
  実在しないパスを残すと自動トリガーやロード条件として機能しない
- モジュールパス参照を含むドキュメントはモジュール移動の際に追従更新が必要。
  主な対象は`docs/development/architecture.md`・`.claude/skills/`配下・`.claude/rules/`配下

## SSOT俯瞰

連動更新先の詳細は各SSOT起点ファイル側のdocstringに集約する。

- `docs/guide/index.md`:
  ty記述・対応ツール一覧（`mkdocs.yml`内llmstxtとは人手同期）
- `docs/guide/usage.md`:
  サブコマンド一覧、出力形式解決の優先順位と許容値、`command-info`節
- `pyfltr/cli/output_format.py`の`resolve_output_format`:
  出力形式解決ロジック本体
- `pyfltr/config/config.py`の`ARCHIVE_CONFIG_KEYS`・`CACHE_CONFIG_KEYS`・`GLOBAL_PRIORITY_KEYS`:
  global優先キーの対象範囲（archive/cache系）
- `pyfltr/config/config.py`の`default_global_config_path`:
  グローバル設定パスの解決ロジック
- `pyfltr/state/archive.py`の`default_cache_root`:
  キャッシュルートの解決ロジック
- `pyfltr/config/config.py`の`DEFAULT_CONFIG`:
  設定キー体系・既定値・runner方針
- `pyfltr/config/config.py`の`is_command_enabled_anywhere`:
  実行対象コマンドの有効（ON/OFF）判定。モノレポでは起点と各サブプロジェクトの和集合で判定する。
  `cli/pipeline.py`の実行対象フィルタ・`state/executor.py`の`split_commands_for_execution`・
  `config/config.py`の`filter_fix_commands`・`output/ui.py`のタブ生成とサマリー行追加で共用する。
  これらの箇所で`config.values.get(cmd)`・`config[cmd]`を直接参照せず本関数を経由する
- `pyfltr/command/runner.py`の`_BIN_TOOL_SPEC` / `build_commandline`:
  mise backend既定値・tool spec組み立て・active tools省略判定
- `pyfltr/command/mise.py`の`MiseActiveToolsResult` / `MiseActiveToolsStatus`:
  mise active tools取得結果の構造とステータス語彙
- `.claude/skills/grep-replace/SKILL.md`:
  grep/replace機能の設計判断・undo方式・CLI/MCP既定値差分
- `docs/guide/custom-commands.md`:
  カスタムコマンド機能の解説。`README.md`特徴章・`docs/guide/index.md`の
  カスタムチェック節およびコンセプト節・`mkdocs.yml`内llmstxtとは人手同期
- ツールの1行概要:
  `README.md`冒頭・`docs/index.md`冒頭・`mkdocs.yml`の`site_description`とllmstxt内
  `markdown_description`・`pyproject.toml`の`description`・`AGENTS.md`冒頭・
  `pyfltr/cli/parser.py`の`description`を人手同期する。
  `site_description`は表示幅に収める短縮形を用いる

`mkdocs.yml`内llmstxtの`markdown_description`にはLLMが利用する際に有用な情報のみ記載する
（全サブコマンド名・主要オプションなど）。
本文は`tests/llmstxt_test.py`が「全サブコマンド名・全ビルトインコマンド名を含むこと」を機械検証するため、
整理時に名前を漏らさず記載する。

## 機能追加時の文書露出判断

既存機能の拡張・新機能追加では、利用者が機能を発見して使用できる掲載経路を整備し、
当該機能が陳腐化させる既存記述を更新する。
個別の対応ツール名単位の掲載先判断は本節の対象外とし、
`.claude/skills/pyfltr-add-tool/SKILL.md`の「触るべきファイル」節の規定に従う。

- 主要機能はREADMEの特徴章へ掲載する。
  既存機能の内部改善など、利用者が新たに選択・操作する要素が無い場合は掲載しない
- 利用者が設定または実行する機能は、`docs/guide/index.md`のコンセプト節と関連する利用者向けページへ掲載する。
  既存ページの手順や仕様に影響しない内部変更は掲載しない
- LLMが機能を選択または操作するために必要な情報は、`mkdocs.yml`内llmstxtの`markdown_description`へ掲載する。
  機能単位の記載漏れは`tests/llmstxt_test.py`で検出されないため人手で検査する
- navでは機能の用途に対応する区分へページを配置する。
  既存ページ内の説明追加だけで機能を発見できる場合は独立ページを追加しない
- 既存文書が回避策・代替手順・適用範囲の限定を案内しており、新機能の挙動と照合した結果、
  当該案内が不要または不正確になる場合は対象記述を更新する。
  検索は新機能が解消する制約を説明している節を起点とし、
  節全体を通読して回避策・代替手順・限定表現を対象とする
