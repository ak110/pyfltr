# 詳細参照サブコマンドと再実行支援

実行アーカイブを参照する`list-runs`/`show-run`サブコマンドと、`--only-failed`/`--from-run`による再実行支援の設計判断。
利用者向けの使い方は[CLIコマンド](../guide/usage.md)を参照。

## 詳細参照サブコマンド

### 実装配置

サブコマンド本体は`pyfltr/runs.py`に集約する。
`main.py`は`generate-config`/`generate-shell-completion`と同じ「非実行系サブパーサー」として
サブパーサー登録とディスパッチのみを行い、出力ロジックは持たない。

読み取り経路は`ArchiveStore`の既存APIを直接利用し、`load_config()`は呼ばない。
キャッシュルートの上書きは環境変数`PYFLTR_CACHE_DIR`のみで完結させて依存を最小化する。

### 実保存ツール一覧の取得

「指定runの実保存ツール一覧」は`tools/`ディレクトリ走査をSSOTとする。
`meta["commands"]`は実行予定のリストで、`--fail-fast`中断や`skipped`で実際には保存されなかったツールを含みうるため。

アーカイブの保存キーはツール名固定のため、同一ツール名のfixステージと通常ステージは
通常ステージで上書きされる。
`show-run`は各ツールの最終保存結果のみを参照可能で、ステージ別保存への拡張は対象外とする。

### run_id解決

完全一致に加えて前方一致と`latest`エイリアスを許容する。
前方一致で複数該当した場合は曖昧と判定し終了コード1を返す。

ULID 26文字を毎回手入力させるUXは現実的でないため完全一致のみとする案は却下した。
解決ロジックは`pyfltr/runs.py`に集約し、MCPサーバー・`--only-failed`からも再利用する。

### 出力フォーマット

`--output-format`は`text`/`json`/`jsonl`の3種とする。
`sarif`/`github-annotations`は実行結果向け形式で詳細参照とは目的が異なるため対象外。

| フォーマット | `list-runs` | `show-run` |
| --- | --- | --- |
| `text` | 固定幅テーブル | 行形式`key: value` |
| `json` | `{"runs":[...]}`の単発dict | 単発dict |
| `jsonl` | 1件1行（`kind: "run"`） | 1件1行（`kind: "meta"`/`"command"`/`"diagnostic"`/`"output"`） |

`json`/`jsonl`モードのみroot loggerを抑止してstdoutを構造化出力で専有する。
`text`モードでは`print`が直接stdoutへ出る（`--verbose`の影響を受けないようにするため`logger.info`は使わない）。

### 終了コード

「アーカイブ未存在の`list-runs`」のみ0（空リスト扱い）。
存在しない`run_id`・前方一致での複数該当・存在しない`--commands`指定はいずれも1を返す。

## --only-failed

直前runから失敗ツールと失敗ファイルを抽出し、ツール別に失敗ファイル集合のみを対象として再実行する。

### `--only-failed`の仕様

- 直前runは`ArchiveStore.list_runs(limit=1)`の先頭を採用する
- 失敗ツール・失敗ファイルはアーカイブのtoolメタとdiagnosticsから抽出する
- 絞り込み結果はツール別の`ToolTargets` dataclass（`pyfltr/only_failed.py`）として保持し、
  各ツールに自分の失敗ファイル集合のみを渡す
- 直前runが存在しない、失敗ツールが無い、ターゲット交差が空となった場合はメッセージを出して成功終了（rc=0）
- 位置引数`targets`との併用時は、直前runの失敗ファイル集合と`targets`を交差させる

### `ToolTargets` dataclass

二状態を型として区別する。

| `mode` | 意味 |
| --- | --- |
| `fallback` | 診断ファイルが無く失敗ファイル特定不可（`pass-filenames=False`で全体失敗）→ 全対象で再実行 |
| `files` | 診断から失敗ファイルを特定済み。`files`が空なら`commands`絞り込みで当該ツールを除外 |

旧形式（`dict[str, list[pathlib.Path] | None]`）では`None`と空リストの違いがコードを読むだけでは不明瞭だった。
`mode`属性で二状態を型として区別し、対象ファイルリスト取得の統一経路を持たせる。

### 挿入位置

絞り込みは`run_pipeline`内のファイル展開直後・archive/cache初期化前に行う。
今回のrunのrun_id/cache_storeに影響させないため。

## --from-run

`--only-failed`の参照対象runをアーカイブの前方一致・`latest`エイリアスで明示指定する。

### `--from-run`の仕様

- `--from-run <RUN_ID>`は`--only-failed`との併用のみを受け付け、単独指定はargparseエラーで拒否する
- `<RUN_ID>`の解決は`pyfltr/runs.py`の`resolve_run_id()`を再利用する（前方一致・`latest`エイリアス・曖昧prefix判定）
- 指定`<RUN_ID>`が存在しない場合は警告を出してrc=0で早期終了する
- 値および`--only-failed`フラグは`retry_command`へ伝播させない

`--from-run`値を`retry_command`へ伝播させる案は却下した。
生成する`retry_command`は「当該ツール＋失敗ファイル」に固定されているため、
アーカイブ参照フラグを引き継ぐと再実行時に古いrunを暗黙参照し続けるリスクがあるため。

`--from-run`を`--only-failed`なしで単独利用可能にする案も却下した。
`--from-run`単独では`diagnostic`参照は行われず意味を持たない。

### MCPサーバーとの関係

`pyfltr/runs.py`のrun_id解決ロジックはMCPサーバー（`mcp_.py`）からも参照される。
複数モジュールからの正規利用経路を1本に揃えるため、解決関数とエラー型はpublicとして公開する。

## 関連設計判断

### アーカイブ保存キーの粒度

アーカイブ保存キーをステージ別（`tools/<command>__<stage>/`形式）にする案は却下した。
影響範囲が大きく、現状の目的（最終保存結果の閲覧）からも外れるため。
必要になった時点で別作業として検討する。
