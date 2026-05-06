# 検索と置換

pyfltrの`grep`/`replace`サブコマンドの使い方を扱う。
コーディングエージェント向けには`pyfltr mcp`の`grep`/`replace`/`replace_undo`ツールも公開する。

## 概要

- `pyfltr grep`: 正規表現でファイル横断検索する。
  pyfltr設定の`exclude`/`extend-exclude`/`respect-gitignore`を尊重するため`node_modules`や`build`配下のノイズが混入しない
- `pyfltr replace`: 横断置換する。書き込みが既定で、世代管理付きの`--undo`で取り消せる
- 両者は共通オプション名（`-i`/`-w`/`-x`/`-F`/`--type`/`-g`等）を共有し、
  `grep`で誤爆ゼロを確認した引数列をそのまま`replace`へ切り替えられる

## grep

### grep基本形

`pyfltr grep <pattern> [paths...]` の形式で実行する。
pathsを省略するとカレントディレクトリ全体が対象となる。

例:

```shell
pyfltr grep "TODO" src/
pyfltr grep -i "deprecated" .
pyfltr grep -F "exact_string" docs/
```

### grepオプション

- `-e/--regexp PATTERN`: 追加パターン（複数指定可、OR結合）
- `-f/--file PATH`: パターンファイル（1行1パターン）
- `-F/--fixed-strings`: 固定文字列モード
- `-i/--ignore-case`: 大文字小文字を区別しない
- `-S/--smart-case`: 大文字を含まないパターンのみignore-caseを有効化
- `-w/--word-regexp`: 単語境界マッチ
- `-x/--line-regexp`: 行全体マッチ
- `-U/--multiline`: マルチラインマッチ
- `-A/-B/-C N`: 前後文脈
- `-m/--max-count N`: ファイル単位の上限
- `--max-total N`: 全体上限（暴発防止用、pyfltr独自）
- `--type TYPE`: 言語タイプフィルタ（python/rust/ts/js/md/json/toml/yaml/shell）
- `-g/--glob PAT`: globフィルタ
- `--encoding ENC`: ファイル読み込みエンコーディング
- `--max-filesize BYTES`: ファイルサイズ上限
- `--no-exclude`/`--no-gitignore`: pyfltr設定の無効化
- `--output-format text|json|jsonl`: 出力形式

### 出力形式

- `text`（既定）: `path:line:col:line_text` 形式
- `json`: 単一JSONとしてmatches配列とsummaryを返す
- `jsonl`: header → match行 → summary行のストリーム

`AI_AGENT`環境変数が設定されている場合は`jsonl`が既定値となる。

### grep→replace連携

grep実行結果のsummary（jsonl形式時）には、同じ引数で`replace`へ切り替える際の案内が含まれる。
誤爆ゼロを確認した引数列をそのまま`replace`へコピーして利用できる。

## replace

### replace基本形

`pyfltr replace <pattern> <replacement> [paths...]` の形式で実行する。
書き込みが既定動作。`--dry-run` で試行できる。

例:

```shell
# 試行（書き込みなし）
pyfltr replace --dry-run "old_name" "new_name" src/
# 実書き込み（履歴に保存される）
pyfltr replace "old_name" "new_name" src/
# 直前のreplaceを取り消す
pyfltr replace --undo <replace_id>
```

`replacement`は`re.sub`互換で、`\1`/`\g<name>`によるキャプチャ参照ができる。

### replaceオプション

- grep側と共通: `-F`/`-i`/`-S`/`-w`/`-x`/`-U`/`--type`/`-g`/`--encoding`/`--max-filesize`/`--no-exclude`/`--no-gitignore`
- replace固有:
    - `--dry-run`: 書き込みせず差分のみ表示
    - `--show-changes`: 各置換箇所の前後行を表示
    - `--exclude-file PATH`: 特定ファイルを置換対象から除外（複数指定可）
    - `--from-grep PATH`: grep出力JSONLを再入力し、対象ファイル集合を限定する
    - `--undo [<replace_id>]`: 過去のreplaceを取り消す（`pattern`位置に履歴IDを渡す）
    - `--force`: undo時のハッシュ不一致を無視して強制復元
    - `--list-history`: 保存済み履歴一覧を表示
    - `--show-history <id>`: 指定履歴の詳細を表示

### undo（取り消し）

書き込み時に世代管理ディレクトリへ「変更前全文・変更後ハッシュ・各置換箇所の前後行」を保存する。
`pyfltr replace --undo <replace_id>` で取り消せる。

ファイルが手動編集されてハッシュが一致しない場合は、デフォルトでスキップされて警告が出る。
意図的に強制復元する場合は `--force` を併用する。

履歴の自動クリーンアップは世代数・合計サイズ・保存期間の3軸で行う。
設定キー（`pyproject.toml`または `pyfltr config set --global` で指定）:

- `replace-history-max-entries`: 最大世代数（既定100件）
- `replace-history-max-size-bytes`: 履歴全体の合計バイト数の上限。
  既定値は`200 * 1024 * 1024`バイト（約200 MiB）
- `replace-history-max-age-days`: 保存期間の上限（既定30日）

### 誤爆除外フロー

1. `pyfltr grep --output-format=jsonl ... > matches.jsonl` でgrepの結果を保存
2. matches.jsonlをエディタで開き、置換対象外のmatch行（の`file`フィールド）を確認
3. 不要ファイルを `--exclude-file=path/to/file.py` で個別除外するか、
   matches.jsonl自体を編集して `--from-grep=matches.jsonl` で渡す

`--from-grep`で読み込むJSONLは、grep実行時のcwd相対でファイルパスを保存している。
このため`replace`を呼ぶときは`grep`実行時と同じcwdから呼ぶ必要がある（cwd差で対象ファイルが
1件もマッチしなくなる事象を避けるため）。
複数プロジェクト横断や別ディレクトリからの呼び出しが必要な場合は、`--exclude-file`で個別の
絶対パスを指定する運用へ切り替える。

マッチ単位除外（`path:line`単位）は当面スコープ外で、ファイル単位で十分な精度を狙う設計。

## MCP公開ツール

`pyfltr mcp`サーバーは`grep`・`replace`・`replace_undo`の3ツールを公開する。

- `grep(pattern, paths, ...)`: ファイル横断検索
- `replace(pattern, replacement, paths, dry_run=True, ...)`: 横断置換。
  **`dry_run`の既定値は`True`**（CLI既定の`False`と異なり、LLM暴発防止）
- `replace_undo(replace_id, force=False)`: 取り消し

LLMエージェントは`replace`を呼ぶ際、明示的に`dry_run=False`を指定しない限り実書き込みされない。
