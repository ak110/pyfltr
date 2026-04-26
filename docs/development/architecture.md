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
細かなクラス・関数構造はソースを参照する。

### CLIエントリ

| モジュール | 責務 |
| --- | --- |
| `main.py` | argparse構築とサブコマンドディスパッチ。`run_pipeline()`本体 |
| `cli.py` | 非TUI実行器 |
| `ui.py` | TUI実行器（Textualベース） |
| `stage_runner.py` | `cli.py`と`ui.py`の共通ヘルパー |
| `executor.py` | プロセスプール管理 |

### コマンド実行

| モジュール | 責務 |
| --- | --- |
| `command.py` | subprocess起動と結果収集 |
| `error_parser.py` | ツール出力の解析 |
| `rule_urls.py` | ツール別の公式ドキュメントURL組み立てテンプレート |
| `precommit.py` | `.pre-commit-config.yaml`の解釈と自動SKIP対応 |
| `command_info.py` | `pyfltr command-info`サブコマンドの解決ロジック |

### 設定とパス処理

| モジュール | 責務 |
| --- | --- |
| `config.py` | `pyproject.toml`読み込みと言語カテゴリゲート処理 |
| `builtin_commands.py` | ビルトインツールの`CommandInfo`定義 |
| `presets.py` | プリセット（`latest`・日付指定）の定義 |
| `paths.py` | パス区切りの統一 |
| `warnings_.py` | JSONL `warning`レコード生成 |

### 出力フォーマット

| モジュール | 責務 |
| --- | --- |
| `llm_output.py` | JSONL出力（5レコード）とsmart truncation |
| `sarif_output.py` | SARIF 2.1.0形式のJSON生成 |
| `github_annotations.py` | GitHub Annotation形式の生成 |
| `code_quality.py` | GitLab CI Code Quality形式の生成 |
| `shell_completion.py` | bash/PowerShell補完スクリプト生成 |

### 実行アーカイブと再実行支援

| モジュール | 責務 |
| --- | --- |
| `archive.py` | 実行アーカイブの書き込み・読み取り・クリーンアップ |
| `cache.py` | ファイルhashベースのスキップキャッシュ |
| `runs.py` | `list-runs`/`show-run`サブコマンドとrun_id解決 |
| `retry.py` | `retry_command`生成ヘルパー |
| `only_failed.py` | `--only-failed`フィルター |

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

subprocess起動は`subprocess.Popen`ベースに統一する。
`--fail-fast`の中断処理（外部スレッドからの`terminate()`呼び出し）が成立する基盤として必要。
パイプライン外で動く`mise --version`・`git check-ignore`・`cls`/`clear`はこの方針の対象外とする。

### `cli.py`/`ui.py`の共通化はヘルパーに絞る

`cli.py`は直接呼び出し、`ui.py`はRich UIへの`call_from_thread`埋め込みという構造差がある。
完全共通化はlock取得タイミング差で実装が複雑になるため、共通化は`stage_runner.py`の小さなヘルパーへの抽出に留める。
残余重複は`# pylint: disable=duplicate-code`を理由コメント付きで維持する。

### ツール解決の失敗扱い

`bin-runner` / `js-runner`によるツール起動解決は、対象ファイル0件のときは省略する。
mise等の解決はネットワーク制約・プラットフォーム制約で失敗し得るため、
解決不要な状況で副作用的な失敗を出さないように早期returnする。

対象ファイルがあるにもかかわらず解決に失敗した場合は、`resolution_failed`という専用ステータスで返す。
通常の実行失敗（`failed`）と区別することで、CIログから
「対象0件で実行をスキップした」のか「対象はあったが解決時点で失敗した」のかを判別可能にする。
exit code判定・`--only-failed`の対象抽出・UI表示はいずれも両者を同等の失敗系として扱う。

### CLI起動時のPATH整理とmise向けenv調整

CLI起動時に`os.environ["PATH"]`の重複エントリを順序先勝ちで除去し、
mise経由のsubprocessにはmiseが注入したtoolパスを除外したPATHを渡す。
親PATHにmise自身のtoolエントリが見つかると、miseがtools解決をスキップしてPATH解決へフォールバックするため、
これを避けるための対症療法である。
詳細な判定ロジックと比較キーは`pyfltr/command.py`の`_build_subprocess_env`を参照。

### `main.py`分割の方針

retry系ヘルパー・`--only-failed`フィルター・パス正規化を専用モジュールへ分離し、`main.py`の肥大化を抑える。
`run_pipeline()`本体は中核として残し、分割対象から外す。

| 移動先 | 内容 |
| --- | --- |
| `retry.py` | retry系ヘルパー（`detect_launcher_prefix()`・`build_retry_args_template()`等） |
| `only_failed.py` | `--only-failed`フィルター本体・`ToolTargets` dataclass |
| `paths.py` | パス区切りの正規化 |
| `stage_runner.py` | `cli.py`/`ui.py`共通の小さなヘルパー |
| `command_info.py` | `pyfltr command-info`サブコマンドの解決ロジック |
