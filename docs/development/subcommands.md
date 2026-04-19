# 詳細参照サブコマンドと再実行支援

実行アーカイブを参照する`list-runs`/`show-run`サブコマンドと、`--only-failed`/`--from-run`による再実行支援の設計判断。
利用者向けの使い方は[CLIコマンド](../guide/usage.md)を参照。

## 詳細参照サブコマンド

### 実装配置

サブコマンド本体は`pyfltr/runs.py`に集約する。
`main.py`は既存の`generate-config`/`generate-shell-completion`と同じ「非実行系サブパーサー」として
サブパーサー登録とディスパッチのみを行い、出力ロジックは持たない。

読み取り経路は`archive.ArchiveStore`の既存APIを直接利用する。
対象は`list_runs`/`read_meta`/`read_tool_meta`/`read_tool_output`/`read_tool_diagnostics`。
`load_config()`は呼ばない。
キャッシュルートの上書きは環境変数`PYFLTR_CACHE_DIR`のみで完結し、サブコマンドの依存を最小化できる。

### 実保存ツール一覧の取得

「指定runの実保存ツール一覧」の取得用に`ArchiveStore.list_tools(run_id)`を提供する。
`meta["commands"]`は実行予定のリストで、`--fail-fast`中断や`skipped`で実際には保存されなかったツールを含みうる。
そのため`tools/`ディレクトリ走査が実保存ツールのSSOTとなる。

アーカイブの保存キーは`tools/<sanitize(command)>/`固定のため、同一ツール名のfixステージと通常ステージは
通常ステージで上書きされる。
`show-run`は各ツールの最終保存結果のみを参照可能で、ステージ別保存への拡張は対象外とする。

### run_id解決

完全一致に加えて前方一致と`latest`エイリアスを許容する。
前方一致で複数該当した場合は曖昧と判定し終了コード1を返す。

ULID 26文字を毎回手入力させるUXは現実的でないため完全一致のみとする案は却下した。
解決ロジックは`pyfltr/runs.py`の`resolve_run_id()`に集約し、`mcp_.py`・`only_failed.py`から再利用する。

### 出力フォーマット

`--output-format`は`text`/`json`/`jsonl`の3種とする。
`sarif`/`github-annotations`は実行結果向け形式で詳細参照とは目的が異なるため対象外。

| フォーマット | `list-runs` | `show-run` |
| --- | --- | --- |
| `text` | 固定幅テーブル | 行形式`key: value` |
| `json` | `{"runs":[...]}`の単発dict | 単発dict |
| `jsonl` | 1件1行（`kind: "run"`） | 1件1行（`kind: "meta"`/`"command"`/`"diagnostic"`/`"output"`） |

`json`/`jsonl`モードのみroot loggerを抑止してstdoutを構造化出力で専有する。
`text`モードではloggerへの影響は無く、`print`が直接stdoutへ出る。
`text`出力も`logger.info`経由にする案は`--verbose`の影響を受けてサブコマンド出力としての一貫性が崩れるため却下した。

### 終了コード

「アーカイブ未存在の`list-runs`」のみ0（空リスト扱い）。
存在しない`run_id`・前方一致での複数該当・存在しない`--tool`指定はいずれも1を返す。

## --only-failed

直前runから失敗ツールと失敗ファイルを抽出し、ツール別に失敗ファイル集合のみを対象として再実行する。

### `--only-failed`の仕様

- 直前run特定は`ArchiveStore.list_runs(limit=1)`の先頭を採用する
- 失敗ツール判定は`read_tool_meta(run_id, tool).status == "failed"`の読み取りで行う
- 失敗ファイル抽出は`read_tool_diagnostics(run_id, tool)`の各エントリの`file`フィールド集合を使う
- 絞り込み結果はツール別の`ToolTargets` dataclassとして保持する
- 絞り込み結果は`run_pipeline` → `cli`/`ui` → `execute_command`の経路で各ツールに
  自分の失敗ファイル集合のみを渡す（ツール間で対象ファイルを共有しない）
- 直前runが存在しない、または失敗ツールが無い場合はメッセージを出して成功終了（rc=0）
- 早期終了判定: `commands`が空、または全ての失敗ツールでターゲット交差が空となった場合に早期終了
- 位置引数`targets`との併用: 直前runの失敗ファイル集合と`targets`を交差させる

### `ToolTargets` dataclass

`mode: Literal["fallback", "files"]`と`files: tuple[pathlib.Path, ...]`を持つfrozen dataclassとして
`pyfltr/only_failed.py`で定義する。
クラスメソッド`fallback_default()`/`with_files(files)`と、インスタンスメソッド`resolve_files(all_files)`を提供する。

| `mode` | 意味 |
| --- | --- |
| `fallback` | 診断ファイルが無く失敗ファイル特定不可（`pass-filenames=False`で全体失敗）→ `all_files`で再実行 |
| `files` | 診断から失敗ファイルを特定済み。`files`が空なら`commands`絞り込みで当該ツールを除外 |

旧`dict[str, list[pathlib.Path] | None]`では`None`と空リストの違いがコードを読むだけでは不明瞭だった。
`mode`属性で二状態を型として区別し、`resolve_files(all_files)`で実際の対象ファイルリストを取得する統一経路を持たせる。
skip状態を3状態目として`ToolTargets`に含める案も検討したが、`commands`絞り込みで除外するため
dictには含めず、値型をシンプルに保つ方針を採った。

### 挿入位置

`apply_filter()`は`run_pipeline`内の`expand_all_files`直後・archive/cache初期化前に挿入する。
今回のrunのrun_id/cache_storeに影響させないため。

### `execute_command()`の引数経路

`execute_command()`の入り口で`only_failed_files: ToolTargets | None`相当の値を受け取る
（実際の引数名は`pick_targets()`経由で解決される失敗ファイル集合）。
`fallback`モードなら既定の`files`引数で実行し、`files`モードならその集合を対象ファイルとして使う。
その後の`target_extensions`フィルタや`pass_filenames=False`の分岐は既存通り適用する。

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

`pyfltr/runs.py`の`resolve_run_id()`/`RunIdError`は`mcp_.py`からも参照される。
private（`_resolve_run_id`/`_RunIdError`）だった旧名はpublic化し、複数モジュールからの正規利用経路を1本に揃える。

## 関連設計判断

### `text`出力でも`print`を使う

`text`モードは`logger.info`経由ではなく`print`を直接使う。
`--verbose`の影響を受けないようにしてサブコマンド出力としての一貫性を保つ。
`json`/`jsonl`モードのみroot loggerを抑止する設計と整合する。

### アーカイブ保存キーの粒度

アーカイブ保存キーを`tools/<command>__<stage>/`形式に変えてステージ別保存にする案は却下した。
`write_tool_result`/`tool.json`/`archive_test.py`等への影響が大きく、現状の目的（最終保存結果の閲覧）からも外れるため。
必要になった時点で別作業として検討する。
