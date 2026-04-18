# 変更履歴

本ドキュメントはpyfltrの主な変更点を記録する。
形式は[Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)に沿い、バージョン番号は[Semantic Versioning](https://semver.org/lang/ja/)に従う。
v3.0.0から整備を開始した。それ以前の変更はgit logおよびGitHubリリースを参照。

## [Unreleased] (3.0.0 予定)

破壊的変更を多く含む。
移行手順は[docs/v3/マイグレーションガイド.md](docs/v3/マイグレーションガイド.md)を参照。

### 破壊的変更

- サブコマンドを必須化。
  引数なし実行時の`ci`フォールバックを廃止し、すべての呼び出しで`pyfltr ci` / `pyfltr run`等を明示する必要がある
- 5ツール（`pyupgrade` / `autoflake` / `isort` / `black` / `pflake8`）を削除。
  ruffへの統合で代替可能となったため、関連設定キー（`pyupgrade-args`等）も受け付けなくなった
- プリセット`"20250710"`を削除。
  削除5ツール向けの設定しか持たなかったため廃止。`preset = "latest"`または`"20260413"`等へ移行する
- Python系ツール（`ruff-format` / `ruff-check` / `mypy` / `pylint` / `pyright` / `ty` / `pytest` / `uv-sort`）をopt-in化。
  既定で無効となり、`python = true`または`{command} = true`で明示的に有効化する
- Python系ツールの依存を`pyfltr[python]`オプショナルグループへ分離。
  利用時は`pip install 'pyfltr[python]'`または`uv add 'pyfltr[python]'`で導入する
- 本体必須依存を`mcp` / `natsort` / `platformdirs` / `python-ulid` / `pyyaml` / `textual`に限定

### 追加

- 実行アーカイブ機能。
  ツール生出力・diagnostic・メタ情報を実行ごとに`platformdirs.user_cache_dir("pyfltr")`配下へ保存する
  - 既定で有効。`--no-archive`や`archive = false`でオプトアウト可能
  - 自動クリーンアップ: 世代数100 / 合計1GB / 30日のいずれかを超過すると古い順に削除
  - 関連設定キー: `archive` / `archive-max-runs` / `archive-max-size-mb` / `archive-max-age-days`
- JSONL出力の拡張
  - `header.run_id`（ULID）: 実行アーカイブの参照キー
  - `diagnostic.rule_url`: 対応ツール（ruff / pylint / pyright / mypy / shellcheck / eslint / markdownlint）のルールドキュメントURL
  - `diagnostic.severity`: `error` / `warning` / `info`の3値に正規化
  - `tool.retry_command`: 1ツール再実行用のshellコマンド文字列
  - `tool.truncated`: smart truncation発生時の切り詰め前情報とアーカイブパス
- smart truncation。
  JSONL出力側でdiagnostic件数および`tool.message`の行数・文字数を制限し、アーカイブには全文を保存する
  - 関連設定キー: `jsonl-diagnostic-limit` / `jsonl-message-max-lines` / `jsonl-message-max-chars`
- 出力形式: `--output-format=sarif`（SARIF 2.1.0互換）と`--output-format=github-annotations`（GitHub Actions向け注釈）
- `--fail-fast`オプション。
  1ツールでもエラーが発生した時点で残りのジョブを打ち切る。
  起動済みサブプロセスには`terminate()`（最大5秒待機 → `kill()`フォールバック）を送り、未開始ジョブは`future.cancel()`で取消して`skipped`として扱う
- ファイルhashキャッシュ機能。
  対象ファイル・設定ファイル未変更時のツール実行をスキップし、過去の結果を復元する
  - 既定で有効。`--no-cache`や`cache = false`でオプトアウト可能
  - 対象は`textlint`のみ（ファイル間依存を持たず、設定ファイルもCWDで完結するlinter）
  - 保存先は実行アーカイブと同じユーザーキャッシュ配下（`<cache_root>/cache/`）
  - キャッシュヒット時はJSONL `tool`レコードに`cached: true` / `cached_from: <ソースrun_id>`を付与
  - 自動クリーンアップ: 期間（既定12時間）超過で削除
  - 関連設定キー: `cache` / `cache-max-age-hours`
- `pyfltr mcp`サブコマンド。
  stdioトランスポートでMCPサーバーを本体同梱で起動する
  - MCPツール群（`list_runs` / `show_run` / `show_run_diagnostics` / `show_run_output` / `run_for_agent`）を提供
  - LLMエージェントからpyfltrの実行と実行アーカイブ参照をMCPツール経由で利用できる
- `pyfltr list-runs`サブコマンド。
  実行アーカイブ内のrun一覧を新しい順で表示する
  - `--limit N`で件数制御（既定20件）、`--output-format=text|json|jsonl`で形式切替
- `pyfltr show-run <run_id>`サブコマンド。
  指定runのmeta・ツール別サマリ・diagnostic・生出力を表示する
  - `<run_id>`は前方一致と`latest`エイリアスを受け付ける（前方一致で複数該当時は曖昧エラー）
  - `--tool NAME`でdiagnostics全件、`--tool NAME --output`で`output.log`全文
  - `--output-format=text|json|jsonl`で形式切替
- `--only-failed`オプション。
  直前runの実行アーカイブから失敗ツールと失敗ファイルを抽出し、ツール別にその組み合わせだけを再実行する
  - 直前runが存在しない・失敗ツールが無い・指定`targets`との交差が空の場合はメッセージを出して`rc=0`で成功終了する
  - 診断ファイルが取得できないツール（pytest等の`pass-filenames=False`系）は既定ファイル展開にフォールバックして全体再実行する
  - 位置引数`targets`との併用時は、直前runの失敗ファイル集合と`targets`の交差を対象にする
- `--from-run <RUN_ID>`オプション。
  `--only-failed`の参照対象runを明示指定する（前方一致・`latest`対応）
  - `--only-failed`との併用が前提で、単独指定はargparseエラーで拒否する
  - 指定`<RUN_ID>`が存在しない場合は警告を出して`rc=0`で早期終了する

### 変更

- `run-for-agent`サブコマンドの既定出力形式を`jsonl`とする（`pyfltr run --output-format=jsonl`と等価）
- `_run_subprocess()`を`subprocess.Popen`ベースに一本化。
  `--fail-fast`による外部スレッドからの`terminate()`を可能にするための実行基盤統一
- JSONL `tool.retry_command`のターゲットを「当該ツールで失敗したファイルのみ」に絞り込む。
  失敗ファイルを特定できない場合（pytest等の`pass-filenames=False`や全体失敗のみ）はターゲット位置引数を空にする。
  キャッシュ復元結果（`cached=true`）では`retry_command`を出力しない
- `pyfltr/main.py`の責務を分割。
  `pyfltr.retry`（retry_command生成）・`pyfltr.only_failed`（`--only-failed`フィルター + `ToolTargets` dataclass）・`pyfltr.paths`（パス正規化）・`pyfltr.stage_runner`（cli/ui共通ヘルパー）へ切り出し
- テキスト出力にrun_idを表示。
  実行ヘッダーログとsummary末尾に`run_id`と`pyfltr show-run latest`誘導文言を追加（archive有効時のみ）
- `--only-failed`の内部データ型を`ToolTargets` dataclassへ整理。
  `mode=fallback/files` を明示し`None`と空リストの曖昧性を排除（内部API、公開インターフェースに影響なし）
