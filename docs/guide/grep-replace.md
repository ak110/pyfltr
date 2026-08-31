# 検索と置換

pyfltrの`grep`/`replace`サブコマンドの使い方を扱う。
コーディングエージェント向けには`pyfltr mcp`の
`grep`/`replace`/`replace_undo`/`replace_history`ツールも公開する。

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
- `--max-preview-chars N`: 返却する本文1件あたりの文字数上限（既定200、0で無制限）
- `--no-exclude`/`--no-gitignore`: pyfltr設定の無効化
- `--output-format text|json|jsonl`: 出力形式

### 出力形式

- `text`（既定）: `path:line:col:line_text` 形式
- `json`: 単一JSONとしてmatches配列とsummaryを返す
- `jsonl`: header → match行 → summary行のストリーム

`AI_AGENT` / `CODEX_CI` / `CLAUDECODE` / `CURSOR_AGENT`環境変数のいずれかが設定されている場合は`jsonl`が既定値となる。

### マッチ本文のプレビュー上限

grepが返す本文（マッチ行`line_text`・マッチ文字列`match_text`・前後コンテキスト）は、
1件あたり既定200文字までのプレビューとして返る。
minifiedファイルやsource mapのような巨大な単一行に一致した場合でも、
1件のマッチが応答全体を占有しない。
この上限はCLIのtext・json・jsonlとMCPの`grep`ツールへ同じ値で適用する。

検索・件数上限（`-m`/`--max-total`）・集計（`-l`/`-c`/`--files-without-match`）は
切り詰め前の全文を対象とするため、上限を変えてもマッチ件数と集計結果は変わらない。

`--max-preview-chars=0`を指定すると切り詰めを行わず、この上限を導入する前と同じ本文が返る。
返るのは`splitlines()`が返す行本文であり、行末の改行文字は従来どおり含まない。

切り詰めが1件でも発生した実行では、経路を問わず警告を返す。
text形式はwarningsセクション、jsonl形式は`kind:"warning"`レコードとsummaryの`warnings`件数、
json形式はpayloadの`warnings`配列とsummaryの`warnings`件数、MCPは戻り値の`warnings`で受け取る。

json形式の`warnings`要素は`source`と`msg`を持ち、対処の手掛かりを伴う警告だけが`hint`を持つ。
jsonl形式の`kind:"warning"`レコードから`kind`を除いたキー集合と同じで、summaryの`warnings`はその要素数を示す。
MCPの`warnings`は警告本文だけを並べた文字列の配列となる。

json形式とjsonl形式のマッチには、切り詰めが発生した場合だけ次のキーが付く。

- `truncated`: 切り詰めが発生したフィールド名の一覧（`line_text`・`match_text`・`before`・`after`）
- `line_text_offset`: `line_text`を切り出した開始位置（0-origin文字数）。
  行頭から切り出した場合は付かない

通常の文字から始まる一致では、`line_text`をマッチ開始位置を含む範囲で切り出すため、
行のどこに一致した場合でも一致箇所が範囲内に入る。
`col`は切り出し前の行における位置を示す。

マルチライン検索で一致が改行から始まる場合、`line`・`col`は正規表現の一致開始位置をそのまま示す。
`line_text`は行末の改行を含まないため、この場合の`col`は`line_text`の行末直後を指し、
切り詰め時は`col - line_text_offset`がプレビュー長より1大きくなる。
実際に一致した改行と後続文字は`match_text`が保持する。

text形式は、マッチ行`line_text`を切り詰めた場合だけ本文の前へ切り出し開始位置を付ける。

```text
src/app.min.js:1:20010:[+19909] ...(200文字のプレビュー)...
```

通常の文字から始まる一致では、`col`から`[+N]`の値を引くと、プレビュー内の位置（1-origin）が求まる。
マッチ行が上限以下の場合は従来どおり`path:line:col:line_text`の形式で、
改行以外から始まる一致の`col`はそのまま`line_text`内の位置を指す。
マッチ文字列や前後の行だけが切り詰められた場合もマッチ行の表示は変わらず、切り詰めは警告で通知する。

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
    - `--within ANCHOR`: アンカー正規表現の行と前後（`-A`/`-B`/`-C`）で定まる領域内のみ置換する
    - `-A`/`-B`/`-C N`: `--within`領域の前後幅（`--within`と併用必須）
    - `--exclude-file PATH`: 特定ファイルを置換対象から除外（複数指定可）
    - `--from-grep PATH`: grep出力JSONLを再入力し、対象ファイル集合を限定する
    - `--undo [<replace_id>]`: 過去のreplaceを取り消す（`pattern`位置に履歴IDを渡す）
    - `--force`: undo時のハッシュ不一致を無視して強制復元
    - `--list-history`: 保存済み履歴一覧を表示
    - `--show-history <id>`: 指定履歴の詳細を表示

### ブロック内限定置換（--within）

`--within`はアンカー正規表現にマッチした行と前後コンテキストで定まる領域内のみを置換対象とする。
領域幅は`-A`/`-B`/`-C`で指定する。sedの範囲アドレスに相当し、特定ブロック外を変更せずに書き換えたいときに使う。

```shell
# 「[section]」を含む行の前後2行の範囲内だけ「old」を「new」へ置換する
pyfltr replace "old" "new" config.toml --within "\[section\]" -C 2
```

- アンカーは検索側フラグ（`-i`/`-w`等）を共用する。アンカー専用のフラグは無い
- `--within`と`-U/--multiline`は併用できない（領域は行範囲で定めるため）
- `-A`/`-B`/`-C`は`--within`と併用必須。`--within`なしで指定するとエラーになる
- 置換件数（`count`）は領域内で実置換した件数で、領域外のマッチは含めない

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

`pyfltr mcp`サーバーは`grep`・`replace`・`replace_undo`・`replace_history`の4ツールを公開する。

- `grep(paths, pattern=None, patterns=None, pattern_file=None, context=None, summary_mode=None, ...)`:
  ファイル横断検索。`patterns`は複数パターン、`pattern_file`は1行1パターンのファイルを受け取り、
  `pattern`とOR条件で検索する。`context`は前後文脈の一括指定に使う。
  `summary_mode`は`files_with_matches`・`count`・`files_without_match`のいずれかを受け取り、
  マッチ明細を空にして対応する集計結果を返す。
  `files_without_match`では全ファイルの確認が必要なため、正の`max_total`を併用できない
  `max_preview_chars`は返却する本文1件あたりの文字数上限（既定200、0で無制限）で、
  切り詰めが発生した場合は`matches[].truncated`・`matches[].line_text_offset`と戻り値の`warnings`で通知する
- `replace(pattern, replacement, paths, dry_run=True, within=None, from_grep=None, context=None, ...)`:
  横断置換。`from_grep`はgrepのJSONL出力から対象ファイルを限定する。
  `context`は`within`で指定したアンカーの前後幅を一括指定する。
  **`dry_run`の既定値は`True`**（CLI既定の`False`と異なり、LLM暴発防止）。
  `within`にアンカー正規表現を渡すと、`before_context`/`after_context`で定まる領域内のみ置換する（CLIの`--within`相当）
- `replace_undo(replace_id, force=False)`: 取り消し
- `replace_history(action, replace_id=None)`: `action=list`で履歴一覧、`action=show`で指定履歴の詳細を返す

LLMエージェントは`replace`を呼ぶ際、明示的に`dry_run=False`を指定しない限り実書き込みされない。
`within`なしで`before_context`/`after_context`/`context`を渡した場合と、
`within`と`multiline`を併用した場合は、いずれもエラーを返す。
