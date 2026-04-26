# 実行アーカイブとファイルhashキャッシュ

pyfltrが提供する2つのユーザーキャッシュ基盤の設計判断と内部仕様。
利用者向けの設定キーや有効/無効化方法は[設定項目](../guide/configuration.md)を参照。

## 共通の前提

両機能は同じユーザーキャッシュディレクトリ配下に保存先を持つ。
保存ルートは`platformdirs.user_cache_dir("pyfltr")`で解決し、
環境変数`PYFLTR_CACHE_DIR`で上書きできる（テスト・運用上の強制上書き用）。
OS別の既定パスは[トラブルシューティング](../guide/troubleshooting.md)を参照。

プロジェクトローカルにキャッシュを作らない方針を採る。
`.gitignore`運用の負担を増やさず、複数プロジェクト横断での参照を可能にするため。

## 実行アーカイブ

### 目的

エージェント連携時にJSONL出力のsmart truncationで削られた情報やツール生出力を事後参照可能にする。
`list-runs`/`show-run`サブコマンドおよびMCPの読み取り系ツール群は本アーカイブを単一の真実源とする。

### ディレクトリ構造

```text
<cache_root>/runs/<run_id>/meta.json
<cache_root>/runs/<run_id>/tools/<sanitize(command)>/output.log
<cache_root>/runs/<run_id>/tools/<sanitize(command)>/diagnostics.jsonl
<cache_root>/runs/<run_id>/tools/<sanitize(command)>/tool.json
```

`meta.json`は実行単位のメタ情報。

- `run_id`（ULID）・`version`・`python`・`executable`・`platform`・`cwd`
- `argv`（起動時引数列）・`commands`（実行対象ツール列）・`files`（対象ファイル数）
- `started_at` / `finished_at`（ISO 8601 UTC）・`exit_code`（finalize後）

`tool.json`はツール単位のメタ情報（`command`/`type`/`status`/`returncode`/`files`/`elapsed`/
`diagnostics`/`has_error`/`commandline`）。
`output.log`はツールの標準出力・標準エラーの結合した生文字列。
`diagnostics.jsonl`は`(command, file)`単位で集約したdiagnosticレコード（`messages[]`配列）を1件1行でJSONLシリアライズしたもの。

### run_id

run_idにはULIDを採用する。UUID v4ではなくULIDを選んだ理由は次の3点。

- タイムスタンプ由来で辞書順ソート＝時系列順ソートとなる。`list_runs`の実装・デバッグが簡潔になる
- 人が見たときに新旧の判別がしやすい
- 十分な衝突耐性

### 自動クリーンアップ

世代数・合計サイズ・保存期間の3軸で制御する。
いずれかの閾値を超過した時点で古い順（run_id昇順）に削除する。

| 設定キー | 既定値 | 0以下を指定した場合 |
| --- | --- | --- |
| `archive-max-runs` | 100 | 世代軸の自動削除を無効化 |
| `archive-max-size-mb` | 1024 | サイズ軸の自動削除を無効化 |
| `archive-max-age-days` | 30 | 期間軸の自動削除を無効化 |

クリーンアップは各実行冒頭で同期実行する。
将来的な非同期化の余地は残すが、現状は単純な同期実行とする。

### 書き込み経路

TUI経路・非TUI経路・JSONL stdout有無のいずれでもアーカイブ書き込みを発生させる。
漏れを防ぐため、ツールの実行結果を受け取った直後の独立フックとして提供する。
JSONL stdoutストリーミングとは独立した経路にすることで、どちらか一方を切り替えても他方が失われない。

### オプトアウト

既定で有効。次のいずれかで無効化できる。

- `--no-archive` CLIオプション
- `archive = false` 設定

無効化時は`ArchiveStore`を生成せず、`archive_hook`も`None`のまま。
JSONLの`header`レコードから`run_id`フィールドも省略される。

オプトイン化（既定無効）は却下した。
エージェント連携時のUXを損なうため、既定有効＋自動削除で肥大化を抑える設計とした。

### diagnosticシリアライズの方針

アーカイブ用のシリアライズはLLM向け出力（`llm_output.py`）と独立した最小構造とする。
`ErrorLocation`の全フィールドを保存することで、`rule_url`等のフィールドが追加された際の追従コストを抑える。

## ファイルhashキャッシュ

### キャッシュの目的

同じ入力に対するツール再実行をスキップし、エージェント連携時の待ち時間と無駄な再計算を削減する。

### 対象ツール

ファイル間依存を持たず、設定ファイルもCWDでのみ解決するlinterに限定する。
対象判定は`CommandInfo.cacheable`フラグで明示する（既定`False`）。
現在の対象は`builtin_commands.py`の`cacheable=True`指定箇所を参照（textlintのみ）。

以下はいずれも対象外とする。

- 書き込み型formatter — ヒット時にファイル書き換えがスキップされ、ファイル状態と結果が不整合になる
- tester — 対象ファイル以外のソース・設定・環境への依存があり、
  依存解析またはプロジェクト全体hashが必要で実装コストに見合わない
- 依存型linter（`mypy`/`ruff-check`/`pylint`等）— import先や型情報のキャッシュが必要で、
  対象ファイル単独のhashでは整合性を保てない
- 外部参照linter（`shellcheck`/`actionlint`）— `source`文やreusable workflowなど外部参照を含みうるため安全側で除外
- 階層型設定を参照するlinter（`ec`/`markdownlint`/`typos`）— 階層型の設定解決は静的列挙では網羅できない

### キャッシュキー

誤ヒットを防ぐため、次の要素をsha256で連結する。

- ツール固有: ツール名・実効コマンドライン・fix段かlint段か・構造化出力の設定値
- 入力依存: 対象ファイル群のsha256・ツール固有設定ファイル群のsha256
- 互換性: pyfltrのMAJORバージョン（メジャー更新で旧キャッシュを無効化）

ツール本体のバージョンはキャッシュキーに含めない。
`pnpx`/`mise @latest`経由で実体が変わりうるが、短期破棄前提（既定12時間）で実害を許容する方針。

### textlintのconfig_files

公式に自動読み込みされる設定ファイル候補を`builtin_commands.py`の`config_files`で完全列挙する。
追加・変更時は同ファイルの`textlint`エントリを更新する。

### 外部ファイル参照引数の安全策

`--{command}-args`に`--config`/`--ignore-path`を含む場合、当該実行ではキャッシュを無効化する（書き込みも読み出しもスキップ）。
指定されたパスを動的に解釈する複雑さを避けるための安全側の実装。

### 保存先とクリーンアップ

`<cache_root>/cache/<tool>/<hash>.json`として保存する。
クリーンアップは期間軸のみ（既定`cache-max-age-hours=12`）。
サイズ・世代数の軸は採用しない（短期破棄前提でストレージ暴発リスクが小さいため）。

### ヒット時の挙動

- ツール実行をスキップして`CommandResult`を完全復元し、`cached=True`/`cached_from=<ソースrun_id>`を設定する
- アーカイブ書き込みは行わない（同じ結果を重複記録しない方針。ソースrunを`cached_from`で参照誘導する）
- JSONLの`command`レコードに`cached`/`cached_from`を出力する
- `retry_command`は出力しない（再実行不要）

### キャッシュのオプトアウト

既定で有効。次のいずれかで無効化できる。

- `--no-cache` CLIオプション
- `cache = false` 設定

実行アーカイブと方針を揃え、エージェント連携時のUXを毀損しないようにする。
