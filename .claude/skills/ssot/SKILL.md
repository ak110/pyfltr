---
name: ssot
description: >
  pyfltrのSSOT・参照パスの方針。
  内部リンクのアンカー方針・MkDocsのdocs_dir外参照禁止・規約適用ルール・モジュールパス参照追従・
  SSOT俯瞰（連動更新先一覧）を集約する。
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
- `pyfltr/command/runner.py`の`_BIN_TOOL_SPEC` / `build_commandline`:
  mise backend既定値・tool spec組み立て・active tools省略判定
- `pyfltr/command/mise.py`の`MiseActiveToolsResult` / `MiseActiveToolsStatus`:
  mise active tools取得結果の構造とステータス語彙
- `.claude/skills/grep-replace/SKILL.md`:
  grep/replace機能の設計判断・undo方式・CLI/MCP既定値差分

`mkdocs.yml`内llmstxtの`markdown_description`にはLLMが利用する際に有用な情報のみ記載する
（`run-for-agent`サブコマンド・主要オプションなど）。
本文は`tests/llmstxt_test.py`が「全サブコマンド名・全ビルトインコマンド名を含むこと」を機械検証するため、
整理時に名前を漏らさず記載する。
