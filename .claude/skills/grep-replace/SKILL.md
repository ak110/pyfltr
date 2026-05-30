---
name: grep-replace
description: >
  pyfltrのgrep / replace機能の方針。
  正規表現置換・dry-run既定値・undo世代管理・MCP grep/replaceツール・除外指定・サブパッケージ命名の
  設計判断を集約する。
  pyfltr/grep_/配下・pyfltr/cli/grep_subcmd.py・replace_subcmd.py・mcp_server.py・
  pyfltr/state/archive.py・tests/grep_*_test.py・tests/replace_subcmd_test.py・
  tests/mcp_test.py・docs/guide/grep-replace.md・docs/guide/usage.md を編集する際に使用する。
---

# pyfltrのgrep / replace機能の方針

## 実装エンジン

実装エンジンは標準ライブラリ`re`に統一する。ripgrep等の外部バイナリ依存は導入しない。
pyfltrの既存ファイル収集機構（`expand_all_files`）とignore設定をそのまま流用し、検索系を内部実装に閉じる。

## ファイル収集の除外

ファイル収集の除外はrun系（`expand_all_files`のexclude / extend-exclude / respect-gitignore）と統一する。
grep / replace固有の追加除外は設けず、ドット始まりのファイルやディレクトリも対象に含める。
直接指定したパスがexcludeパターンや`.gitignore`で対象外になった場合は、
warningとsummaryの`fully_excluded_files` / `missing_targets`で通知し、無言のスキップを避ける。

## 引数体系の同一性

`grep`と`replace`は共通オプション名（`-i`/`-w`/`-x`/`-F`/`--type`/`-g`等）を共有する。
利用者が`grep`で確認した引数列を`replace`へそのまま転用できる設計とする。
両コマンド固有のオプション（grepの`-l`/`-c`等、replaceの`--dry-run`/`--undo`等）は
片方でのみ受理し、もう片方では拒否する。

例外として`-A`/`-B`/`-C`は両コマンドで受理するが意味が異なる。
grepではマッチ前後のコンテキスト表示幅を指定し、replaceでは`--within`領域の前後幅を指定する。
意味差による誤動作を防ぐため、replaceでは`--within`を伴わない`-A`/`-B`/`-C`を拒否する。

## MCPでのdry-run既定値

MCPツール`replace`の`dry_run`既定値は`True`とする（CLIの既定値`False`と異なる）。
LLMエージェントは引数の効果を完全に理解せず実行する場面があり、暴発的な変更を防ぐため
明示的に`dry_run=False`が指定されたときのみ実書き込みを許可する。
CLI利用者は対話的に意図を確認する前提のためCLI既定値は実書き込みを維持する。

## MCPでのmax_total既定値

MCPツール`grep`の`max_total`既定値は`1000`とする（CLIの既定値`0`=無制限と異なる）。
LLM暴発時の検索結果フラッディング（広域パターンの大量マッチ）を防ぎ、コンテキスト消費を安全側に抑える。
利用者は明示的に`max_total=0`を渡せば無制限へ切り替えできる。
CLI利用者は端末上で逐次確認できる前提のためCLI既定値は無制限を維持する。

## undo世代の管理方式

replaceの実行アーカイブは`pyfltr/state/archive.py`と同じくユーザーキャッシュ配下に世代管理する。
保存単位は`replace_id`（ULID）で、保存内容は次の3点。

- 変更前全文（undo時の復元元）
- 変更後全文ハッシュ（undo時の手動改変検出に使う）
- 各置換箇所の前後行（`--show-changes`・show-replace表示用）

自動クリーンアップは世代数・合計サイズ・保存期間の3軸で行う。
既定の上限値は実行アーカイブと同程度に揃える。

## undo時の手動改変検出

`pyfltr replace --undo`は対象ファイルの現状ハッシュと保存された変更後ハッシュを照合する。
不一致時は警告を発して中断し、`--force`指定時のみ強制復元する。
replace後に手動編集された変更を意図せず巻き戻す事故を防ぐ。

## 除外指定の粒度

誤爆ファイル除外は`--exclude-file=PATH`（複数指定可）と`--from-grep=PATH`（grep出力JSONLを再入力）
の組み合わせで提供する。マッチ単位除外（`path:line`単位）は実装複雑度に見合う利点が乏しいためスコープ外とする。
精緻な除外が必要な場合はgrepのfileリストを手動編集して`--exclude-file`へ渡す運用で対応する。

## ブロック内限定置換

`replace`の`--within ANCHOR`はアンカー正規表現にマッチした行と前後コンテキスト（`-A`/`-B`/`-C`）で
定まる行範囲集合に限定して置換する。sedの範囲アドレスに相当する。
これは行範囲集合への限定であり、スコープ外とした`path:line`単位のマッチ除外とは別軸の機能とする。

設計判断は次のとおり。

- アンカーは検索側のフラグ（`-i`/`-w`等）を共用し、アンカー専用のフラグ群は設けない。
- `--within`は単一アンカー正規表現のみ受理する。`grep`の`-e`/`-f`相当の複数指定は導入しない。
- `--within`と`-U/--multiline`は併用不可とする。領域を行範囲で定めるため、
  行境界を跨ぐマルチライン検索は領域内置換の意味が崩れる。
- 領域を切り出してから置換するのではなく、ファイル全文に対して検索し、
  マッチ範囲が許可行範囲へ完全包含されるもののみ置換する。
  これにより`^`/`$`/`\A`/`\Z`/前後読みの評価対象が全体置換と一致する。
- `/start/,/end/`形式の2正規表現による範囲指定は採用せず、行数基準（`-A`/`-B`/`-C`）に限定する。

## サブパッケージ命名

新規サブパッケージは`pyfltr/grep_/`としてサフィックス`_`を付ける。
`pyfltr/cli/mcp_server.py`の`mcp`パッケージ衝突予防と同じ方針で、標準ライブラリ`re`との混同を避ける。
