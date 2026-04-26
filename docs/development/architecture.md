# アーキテクチャ概要

pyfltrの実装構造とモジュール責務分離の設計判断をまとめる。
本ページは保守・拡張に携わる開発者向け。
利用者向けの機能解説は[CLIコマンド](../guide/usage.md)・[設定項目](../guide/configuration.md)を参照。

## 実行パイプライン

`pyfltr.main.run_pipeline()`がCLI/MCPの双方から呼び出される最上位エントリ。
TUI/非TUIの分岐はこの関数の内側で行い、パイプライン共通の前処理（ファイル展開・`--only-failed`絞り込み・
アーカイブ初期化など）はTUI起動より前に集約する。

実行ステージは次の3段で構成する。

1. fixステージ — `{command}-fix-args`が定義された有効なlinterを順次`--fix`付きで実行する（`ci`サブコマンドは無効）
2. formatterステージ — `ruff-format`・`prettier`等のformatterを直列または並列で実行する
3. linter/testerステージ — 残りのlinter/testerを並列実行する

各ステージの結果は`CommandResult`に集約され、ステージ完了ごとに`archive_hook`へ渡される。
ステージ間の中断（`--fail-fast`時の打ち切りなど）は`stage_runner`の共通ヘルパーで吸収する。

## モジュール構成

`run_pipeline()`から呼び出される主要モジュールと責務を示す。

### CLIエントリ

| モジュール | 責務 |
| --- | --- |
| `main.py` | argparse構築・サブコマンドディスパッチ・`run_pipeline()`本体・retry_command生成委譲 |
| `cli.py` | 非TUI実行器。3ステージのループとログ出力を担う |
| `ui.py` | TUI実行器。Textualベース。`call_from_thread`を介して非同期にステージを駆動する |
| `stage_runner.py` | `cli.py`/`ui.py`共通の小さなヘルパー（`make_skipped_result()`・`cancel_pending_futures()`） |
| `executor.py` | プロセスプール管理 |

### コマンド実行

| モジュール | 責務 |
| --- | --- |
| `command.py` | `execute_command()`（subprocess起動・ファイル渡し・結果収集）と`CommandResult`データクラス |
| `error_parser.py` | ツール出力の解析と`ErrorLocation`生成 |
| `rule_urls.py` | ツール別の公式ドキュメントURL組み立てテンプレート |
| `precommit.py` | `.pre-commit-config.yaml`の解釈（自動SKIP対応） |

### 設定とパス処理

| モジュール | 責務 |
| --- | --- |
| `config.py` | `pyproject.toml`読み込み・`CommandInfo`定義・プリセット・言語カテゴリゲート処理 |
| `paths.py` | `normalize_separators()`によるパス区切りの統一 |
| `warnings_.py` | JSONL `warning`レコード生成 |

### 出力フォーマット

| モジュール | 責務 |
| --- | --- |
| `llm_output.py` | JSONL出力（`header`/`diagnostic`/`command`/`warning`/`summary`の各レコード生成）・smart truncation |
| `sarif_output.py` | SARIF 2.1.0形式のJSON生成 |
| `github_annotations.py` | GitHub Annotation形式（`::error file=...`等）の生成 |
| `code_quality.py` | GitLab CI Code Quality形式（Code Climate JSON issueサブセット）の生成 |
| `shell_completion.py` | bash/PowerShell補完スクリプト生成 |

### 実行アーカイブと再実行支援

| モジュール | 責務 |
| --- | --- |
| `archive.py` | `ArchiveStore`（書き込み・読み取り・クリーンアップ）・`policy_from_config()` |
| `cache.py` | `CacheStore`（ファイルhashベースのスキップキャッシュ）・`is_cacheable()`・`resolve_config_files()` |
| `runs.py` | `list-runs`/`show-run`サブコマンド本体・`resolve_run_id()`（前方一致・`latest`エイリアス） |
| `retry.py` | `retry_command`生成ヘルパー群（`detect_launcher_prefix()`・`build_retry_args_template()`等） |
| `only_failed.py` | `--only-failed`フィルター本体・`ToolTargets` dataclass |

### MCPサーバー

| モジュール | 責務 |
| --- | --- |
| `mcp_.py` | FastMCPサーバー本体・MCPツール5種・stdio隔離 |

## サブコマンドとargparse

argparse subparsers（`required=True`）でサブコマンドを必須化し、引数なし実行時のフォールバック挙動は持たない。
共通オプションは`parents=[common]`で各サブパーサーへ継承する。
サブコマンド別の既定値（`exit_zero_even_if_formatted`・`commands`・`output_format`・`include_fix_stage`）は
`_apply_subcommand_defaults()`で手動注入する。
`set_defaults()`経由の注入を避けたのは、共通親パーサーを継承しているサブパーサーに対して
他サブパーサーのdefaultが書き換わる既知挙動を回避するため。

サブコマンド一覧と用途は[CLIコマンド](../guide/usage.md)を参照。

## 主要な設計判断

### 言語カテゴリはゲートとして働く

`python` / `javascript` / `rust` / `dotnet`の各言語カテゴリに属するツールは既定で無効（カテゴリキーが既定`false`）。
対象外プロジェクトで意図しないツール実行が起こることを避けるため、対応する言語カテゴリキー（例: `python = true`）で
ゲートを開けるか、`{command} = true`の個別明示が必要。

プリセットは各時点の推奨ツール構成を示すスナップショットで言語別ツールも含むが、
カテゴリキーが`false`のままだとプリセット由来の該当ツール`true`をゲート処理で`false`へ押し戻す。
個別`{command} = true`はゲートを越えて最優先される。
Python系の追加依存は`pyfltr[python]`オプショナルグループに分離する。
JavaScript / Rust / .NET系は各言語のツールチェイン（Node.js・cargo・dotnet CLI）が前提のため、
pyfltr本体はこれらの依存を抱えない。

代替案として「完全別パッケージ（`pyfltr-python`）に分離」も検討したが、リポジトリ・リリース・バージョン整合の
複雑度が増し、ユーザー体験もextras方式より劣るため不採用とした。

### 必須依存は最小化

本体必須依存は次の役割に限定する。

- 骨組み: `textual`（TUI）・`natsort`（自然順ソート）・`pyyaml`（pre-commit設定）
- run_id生成: `python-ulid`
- MCP同梱: `mcp`・`platformdirs`
- プロセス判定: `psutil`（`git commit`経由起動を親系列で検出してMM状態ガイダンスを出す用途）

`mcp`を本体必須に含めるのはサーバー同梱体験（`pyfltr mcp`が即座に使える）を保つため。

### subprocess実行はPopen一本化

`command._run_subprocess()`は`subprocess.Popen`ベースに統一する。
`--fail-fast`の中断処理（外部スレッドからの`terminate()`呼び出し）が成立する基盤として必要。
`_active_processes`は`threading.Lock`で排他し、追加・削除と中断の衝突を防ぐ。

`_resolve_bin_commandline()`の`mise --version`、`_filter_by_gitignore()`の`git check-ignore`、
`main.py`の`cls`/`clear`はパイプライン外のため対象外。

### `cli.py`/`ui.py`の共通化はヘルパーに絞る

`cli.py`は`call_from_thread`を持たない直接呼び出し、`ui.py`はRich UIへの`call_from_thread`埋め込みという構造差がある。
完全共通化はlock取得タイミング差で実装が複雑になるため、
`make_skipped_result()`・`cancel_pending_futures()`の2関数を`stage_runner.py`へ抽出するに留める。
残余重複は`# pylint: disable=duplicate-code`を理由コメント付きで維持する。

### ツール解決の失敗扱い

ツール起動コマンドの解決（`bin-runner` / `js-runner` 経由）は、対象ファイル0件のときは省略する。
mise等のbin-runner解決はネットワーク制約・プラットフォーム制約で失敗し得るため、
解決不要な状況で副作用的な失敗を出さないよう早期returnして空の実行パラメータを返す。

対象ファイルがあるにもかかわらず解決に失敗した場合は、`resolution_failed`という専用ステータスを返す。
通常の実行失敗（`failed`）とは独立に集計・表示することで、CIログから
「対象0件で実行をスキップしたのか／対象はあったが解決時点で失敗したのか」を判別可能にする。
exit code判定・`--only-failed`の対象抽出・UI表示はいずれも`failed`と`resolution_failed`を同等の失敗系として扱う。

### `main.py`分割の方針

retry系ヘルパー・`--only-failed`フィルター・パス正規化を専用モジュールへ分離し、`main.py`の肥大化を抑える。
`run_pipeline()`本体は中核として残し、分割対象から外す。

| 移動先 | 内容 |
| --- | --- |
| `retry.py` | `detect_launcher_prefix()`・`build_retry_args_template()`等のretry系5関数と`_VALUE_OPTIONS` |
| `only_failed.py` | `apply_filter()`（5サブ関数に責務分割）・`ToolTargets` dataclass |
| `paths.py` | `normalize_separators()` |
| `stage_runner.py` | `cli.py`/`ui.py`共通の2ヘルパー |
