# アーキテクチャ概要

pyfltrの実装構造と主要な設計判断をまとめる。
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

## モジュール構成 {#modules}

`run_pipeline()`から呼び出される主要モジュール群を責務カテゴリごとに示す。
各モジュールの細かな関数・クラス構造はソースを直接参照する。

- CLIエントリ: `main.py`（argparse構築・`run_pipeline()`本体）・`cli.py`（非TUI実行器）・
  `ui.py`（Textual TUI実行器）・`stage_runner.py`（CLI/TUI共通ヘルパー）・`executor.py`（プロセスプール管理）
- コマンド実行: `command.py`（subprocess起動と結果収集）・`error_parser.py`（ツール出力解析）・
  `rule_urls.py`（ツール別公式ドキュメントURL）・`precommit.py`（`.pre-commit-config.yaml`解釈）・
  `command_info.py`（`pyfltr command-info`サブコマンド）
- 設定とパス処理: `config.py`（`pyproject.toml`読み込み・言語カテゴリゲート）・
  `builtin_commands.py`（ビルトインツールの`CommandInfo`定義）・`presets.py`（プリセット）・
  `paths.py`（パス区切りの統一）・`warnings_.py`（JSONL `warning`レコード生成）
- 出力フォーマット: `llm_output.py`（JSONLとsmart truncation）・`sarif_output.py`（SARIF 2.1.0）・
  `github_annotations.py`（GitHub Annotation）・`code_quality.py`（GitLab CI Code Quality）・
  `shell_completion.py`（bash/PowerShell補完スクリプト生成）
- 実行アーカイブと再実行支援: `archive.py`（書き込み・読み取り・クリーンアップ）・
  `cache.py`（ファイルhashベースのスキップキャッシュ）・`runs.py`（`list-runs`/`show-run`）・
  `retry.py`（`retry_command`生成）・`only_failed.py`（`--only-failed`フィルター）
- MCPサーバー: `mcp_.py`（FastMCPサーバー本体・MCPツール5種・stdio隔離）

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

retry系ヘルパー・`--only-failed`フィルター・パス正規化・TUI/CLI共通ヘルパーを専用モジュールへ分離し、
`main.py`の肥大化を抑える。
`run_pipeline()`本体は中核として残し、分割対象から外す。

具体的な分割先と内容は[モジュール構成](#modules)を参照。

## 実行アーカイブとファイルhashキャッシュ {#archive-and-cache}

pyfltrは2系統のユーザーキャッシュ基盤を持つ。
利用者向けの設定キーは[設定項目](../guide/configuration.md)を、OS別の既定パスは
[トラブルシューティング](../guide/troubleshooting.md)を参照。

保存ルートは`platformdirs.user_cache_dir("pyfltr")`で解決し、環境変数`PYFLTR_CACHE_DIR`で上書きできる。
プロジェクトローカルにキャッシュを作らない方針を採るのは、`.gitignore`運用の負担を増やさず、
複数プロジェクト横断での参照を可能にするため。

### 実行アーカイブ

エージェント連携時にJSONL出力のsmart truncationで削られた情報やツール生出力を事後参照可能にする。
`list-runs`/`show-run`サブコマンドおよびMCPの読み取り系ツール群は本アーカイブを単一の真実源とする。

run_idにはULIDを採用する。タイムスタンプ由来で辞書順ソート＝時系列順ソートとなり`list-runs`の実装が簡潔になる、
人が見たときに新旧の判別がしやすい、十分な衝突耐性を持つ、の3点が選定理由。

自動クリーンアップは世代数（`archive-max-runs`）・合計サイズ（`archive-max-size-mb`）・
保存期間（`archive-max-age-days`）の3軸で制御する。
いずれかの閾値を超過した時点で古い順（run_id昇順）に削除する。
各設定値に0以下を指定すると当該軸の自動削除が無効化される。

書き込みはツール実行結果を受け取った直後の独立フックとして提供し、TUI経路・非TUI経路・
JSONL stdout有無のいずれでも発生する。
JSONL stdoutストリーミングとは独立した経路にすることで、どちらか一方を切り替えても他方が失われない。

既定で有効。`--no-archive`または`archive = false`設定で無効化できる。
オプトイン化（既定無効）は却下した。
エージェント連携時のUXを損なうため、既定有効＋自動削除で肥大化を抑える設計とした。

アーカイブ用のシリアライズはLLM向け出力（`llm_output.py`）と独立した最小構造とし、
`ErrorLocation`の全フィールドを保存する。
`rule_url`等のフィールドが追加された際の追従コストを抑える狙い。

### ファイルhashキャッシュ

同じ入力に対するツール再実行をスキップし、エージェント連携時の待ち時間と無駄な再計算を削減する。
対象は「ファイル間依存を持たず、設定ファイルもCWDでのみ解決するlinter」に限り、
`CommandInfo.cacheable=True`で明示する（現状はtextlintのみ）。

キャッシュキーには次の要素をsha256で連結する。

- ツール固有: ツール名・実効コマンドライン・fix段かlint段か・構造化出力の設定値
- 入力依存: 対象ファイル群のsha256・ツール固有設定ファイル群のsha256
- 互換性: pyfltrのMAJORバージョン

誤ヒット防止が目的であり、ツール本体のバージョンは含めない（短期破棄前提で実害を許容）。

ヒット時はツール実行をスキップして`CommandResult`を完全復元し、`cached=True`/`cached_from=<ソースrun_id>`を設定する。
アーカイブ書き込みは行わず（同じ結果を重複記録しない）、`retry_command`も出力しない（再実行不要のため）。

`<cache_root>/cache/<tool>/<hash>.json`形式で保存する。
クリーンアップは期間軸（既定`cache-max-age-hours=12`）のみ。
サイズ・世代数の軸は採用しない（短期破棄前提でストレージ暴発リスクが小さいため）。

既定で有効。`--no-cache`または`cache = false`設定で無効化できる。

カテゴリ別の対象外判定とその根拠は`pyfltr/cache.py`モジュール冒頭docstringを参照。
formatter・tester・依存型linter・外部参照linter・階層型設定linterの5分類を扱う。
`--config`/`--ignore-path`検知時の安全側無効化も同所に記載する。

## 出力フォーマット {#output-formats}

`--output-format`は`text`（既定）・`jsonl`・`sarif`・`github-annotations`・`code-quality`の5種を持つ。
利用者向けのレコード書式は[CLIコマンド](../guide/usage.md#jsonl)を参照。
本節では設計判断を中心に扱う。

### LLM向けガイダンス

JSONLはLLMエージェントが入力として読むケースが多いため、フィールドの意味と失敗時の次アクションを
英語で明示的に同梱する。
英語にするのはトークン効率（日本語より短くなりやすい）と汎用性（LLMの入力として標準的）のため。

- `header.schema_hints`: 毎runに付与する英語の辞書。
  既定はLLMが読み違いやすい非自明フィールドのみに絞った短縮版、`-v` / `--verbose`指定時に
  フィールド単位の詳細を含むフル版に切り替わる
- `summary.applied_fixes`: fixステージ・formatterステージで実際にファイル内容が変化した対象のパス一覧
- `summary.guidance`: `failed + resolution_failed > 0`のときのみ付与する英語の配列。
  `command.retry_command`の参照、`--only-failed`再実行、`diagnostic.fix`の解釈、
  `pyfltr show-run <run_id>`の案内を並べる

### hint-urls集約

ルールドキュメントURLは`diagnostic`本体に含めず、`command`レコード末尾の`hint-urls`辞書に集約する。
キーはrule ID、値はURL。
URLを生成できたruleのみ含み、1件も無ければフィールドごと省略する。
外部に出るキー名は常に`hint-urls`（ハイフン）で統一する。
Python/Pydantic内部では`hint_urls`（アンダースコア）で扱い、`mcp_.py`の`ToolDiagnosticsModel`は
Pydanticのaliasで吸収する。

### retry_command

当該ツール1件を再実行するshellコマンド文字列で、`command`レコードに埋め込む。
構成要素は次の3点。

- 起動プレフィックス: 親プロセスから`uv run pyfltr`/`uvx pyfltr`/`pyfltr`を判定する。
  Linuxでは`/proc/self/status`経由、macOS/Windowsではargv basenameへフォールバックする
- ベーステンプレート: 起動時のargvをコピーし、`--commands`値を当該ツールへ差し替え、位置引数を除去する
- ターゲット: 当該ツールで失敗したファイルを絶対パス化して末尾に追加する。
  `--work-dir`適用前の元cwdを基準とすることで、再実行時のcwd二重解釈を避ける

このため`pyfltr ci`失敗時の`retry_command`に`pyfltr run`が混入してfixステージが暴発することは無い。
キャッシュ復元結果（`cached=True`）では`retry_command`を埋めない。

### smart truncationとアーカイブ復元

JSONL側で次の上限を適用する（`pyproject.toml`で調整可能）。

- `jsonl-diagnostic-limit`: 1ツールあたりの出力上限（集約前の個別指摘の合計で判定）。既定`0`（無制限）
- `jsonl-message-max-lines`: `command.message`（生出力末尾）の行数上限。既定`30`
- `jsonl-message-max-chars`: `command.message`の文字数上限。既定`2000`

切り詰めの可否はアーカイブ書き込み成功フラグで判定する。
書き込み成功時のみ切り詰めを適用し、失敗時は全文をJSONLに出力する（復元不能な情報欠落の防止）。
fixステージと通常ステージを区別する必要があるため、判定単位はステージごとのCommandResult単位とする。

`command.message`の切り詰めはハイブリッド方式で行う。
書式は「先頭ブロック + 中略マーカー `\n... (truncated)\n` + 末尾ブロック」。
冒頭にエラー要約を出すツール（editorconfig-checker等）と、末尾にスタックトレースを出すツール
（pytest・mypy等）の双方を救う狙いがある。

### SARIF / GitHub Annotation / Code Quality

`severity`からの変換マップ。

- SARIF level: `error`→`"error"` / `warning`→`"warning"` / `info`→`"note"` / 未設定→`"warning"`
- GitHub Annotation: `error`→`::error` / `warning`→`::warning` / `info`→`::notice` / 未設定→`::warning`
- Code Quality: `error`→`"major"` / `warning`→`"minor"` / `info`→`"info"` / 未設定→`"minor"`

Code Qualityの仕様は5段階（`info` / `minor` / `major` / `critical` / `blocker`）だが、
pyfltr側に対応情報が無く過大評価を避けるため上位2段階は使わない。

GitHub Annotationの`title`は`{tool}: {rule}`形式（ruleが無ければtool名のみ）。
本文は仕様に沿って`%`/改行をパーセントエンコードする。

Code Qualityの`fingerprint`はtool・file・line・col・rule・msgをタブ区切りで連結した文字列の
SHA-256全桁を採用する。
同一指摘の重複統合に足るユニーク性を確保しつつ、配置順の変化に頑強にする。

### logger 3系統と出力形式 {#logger}

pyfltrは3系統のloggerを使い分ける。

- root（system logger）: 常にstderr。抑止しない。設定エラー・アーカイブ初期化失敗などを流す
- `pyfltr.textout`: 人間向けテキスト出力（進捗・詳細・summary・warnings・`--only-failed`案内）
- `pyfltr.structured`: 構造化出力（JSONL / SARIF / Code Quality）

`pyfltr.textout`のformat別振る舞い（`pyfltr.cli.configure_text_output`で設定）。

| `output_format` | `output_file` | text stream | text level |
| --- | --- | --- | --- |
| `text`（既定） | 任意 | stdout | INFO |
| `github-annotations` | 任意 | stdout | INFO |
| `jsonl` | 未指定 | stderr | WARN |
| `jsonl` | 指定 | stdout | INFO |
| `sarif` | 未指定 | stderr | INFO |
| `sarif` | 指定 | stdout | INFO |
| `code-quality` | 未指定 | stderr | INFO |
| `code-quality` | 指定 | stdout | INFO |
| 任意 | 任意（MCP経路） | stderr | INFO |

`pyfltr.structured`のhandler設定（`pyfltr.cli.configure_structured_output`で設定）。

- `jsonl` / `sarif` / `code-quality` + `--output-file`未指定 → `StreamHandler(sys.stdout)`
- `jsonl` / `sarif` / `code-quality` + `--output-file`指定 → `FileHandler(output_file, mode="w", encoding="utf-8")`
- `text` / `github-annotations` → handler未設定（構造化出力は発生しない）

stdout占有が起きるのは`jsonl` / `sarif` / `code-quality`かつ`--output-file`未指定時のみ。
MCP経路（`pyfltr.mcp_.run_for_agent`）は同一プロセス内で`run_pipeline(..., force_text_on_stderr=True)`
を直接呼び、textloggerをstderrに強制する。
構造化出力は一時ファイル経由（FileHandler）となりstdoutを汚染しない。

## 詳細参照サブコマンドと再実行支援 {#subcommands}

実行アーカイブを参照する`list-runs`/`show-run`サブコマンドと、`--only-failed`/`--from-run`による
再実行支援の設計判断。
利用者向けの使い方は[CLIコマンド](../guide/usage.md)を参照。

### `list-runs`/`show-run`の実装配置

サブコマンド本体は`pyfltr/runs.py`に集約する。
`main.py`は`generate-config`/`generate-shell-completion`と同じ「非実行系サブパーサー」として
サブパーサー登録とディスパッチのみを行い、出力ロジックは持たない。

読み取り経路は`ArchiveStore`の既存APIを直接利用し、`load_config()`は呼ばない。
キャッシュルートの上書きは環境変数`PYFLTR_CACHE_DIR`のみで完結させて依存を最小化する。

「指定runの実保存ツール一覧」は`tools/`ディレクトリ走査をSSOTとする。
`meta["commands"]`は実行予定のリストで、`--fail-fast`中断や`skipped`で実際には保存されなかったツールを
含みうるため。

アーカイブの保存キーはツール名固定のため、同一ツール名のfixステージと通常ステージは
通常ステージで上書きされる。
`show-run`は各ツールの最終保存結果のみを参照可能で、ステージ別保存への拡張は対象外とする。

run_id解決は完全一致に加えて前方一致と`latest`エイリアスを許容する。
解決ロジックは`pyfltr/runs.py`の`resolve_run_id()`に集約し、
MCPサーバー・`--only-failed`からも再利用する。

### `--only-failed`

直前runから失敗ツールと失敗ファイルを抽出し、ツール別に失敗ファイル集合のみを対象として再実行する。

- 直前runは`ArchiveStore.list_runs(limit=1)`の先頭を採用する
- 失敗ツール・失敗ファイルはアーカイブのtoolメタとdiagnosticsから抽出する
- 絞り込み結果はツール別の`ToolTargets` dataclass（`pyfltr/only_failed.py`）として保持する
- 直前runが存在しない、失敗ツールが無い、ターゲット交差が空となった場合はメッセージを出して
  成功終了（rc=0）する
- 位置引数`targets`との併用時は、直前runの失敗ファイル集合と`targets`を交差させる

絞り込みは`run_pipeline`内のファイル展開直後・archive/cache初期化前に行う。
今回のrunのrun_id/cache_storeに影響させないため。

### `--from-run`

`--only-failed`の参照対象runをアーカイブの前方一致・`latest`エイリアスで明示指定する。

- `--from-run <RUN_ID>`は`--only-failed`との併用のみを受け付け、単独指定はargparseエラーで拒否する
- `<RUN_ID>`の解決は`pyfltr/runs.py`の`resolve_run_id()`を再利用する
- 指定`<RUN_ID>`が存在しない場合は警告を出してrc=0で早期終了する
- 値および`--only-failed`フラグは`retry_command`へ伝播させない

`--from-run`値は`retry_command`へ伝播させない方針を採る。
生成する`retry_command`は「当該ツール＋失敗ファイル」に固定されているため、
アーカイブ参照フラグを引き継ぐと再実行時に古いrunを暗黙参照し続けるリスクがある。

`--from-run`を`--only-failed`なしで単独利用可能にする案も却下した。
`--from-run`単独では`diagnostic`参照は行われず意味を持たない。

## MCPサーバー {#mcp-server}

`pyfltr mcp`サブコマンドが提供するMCP（Model Context Protocol）サーバーの設計判断。
利用者向けの起動方法・MCPツール一覧・MCPクライアント設定例は[CLIコマンド](../guide/usage.md)を参照。

### 提供ツール構成

読み取り系4ツール（`list_runs`・`show_run`・`show_run_diagnostics`・`show_run_output`）と
実行系1ツール（`run_for_agent`）の計5ツールを公開する。
実行系を1本に絞ったのは、エージェント連携用途では`ci`/`run`/`fast`の差分を露出する必要が薄く、
パラメーター数を抑えてMCPスキーマを単純化するため。

ツール名はCLIサブコマンドのハイフン形式と異なりアンダースコア形式（`list_runs`/`show_run`等）とする。
ハイフンはPythonの`@mcp.tool()`名として非推奨のため。

### MCPライブラリ

`mcp.server.fastmcp.FastMCP`を採用する。
高レベルDSLで記述量が最小、型ヒントからinputSchemaとoutputSchemaを自動生成可能、
stdioトランスポート起動が`mcp.run(transport="stdio")`の一行で済む点が決め手となった。
低レベルAPI（`mcp.server.Server`）の利点が必要となる動的capability交渉は不要。

### stdio隔離

stdioトランスポートはstdin/stdoutをJSON-RPCフレームに専有するため、
どの経路であれstdoutへの書き込みはプロトコル破壊を引き起こす。
3層で隔離を実施する。

1. 起動直後にroot loggerの出力先をstderrへ強制する
2. `run_for_agent`ツール内では`run_pipeline`に`force_text_on_stderr=True`を渡し、
   人間向けtext整形loggerをstderrへ向ける。構造化出力は一時ファイルへFileHandler経由で書き出す
3. TUI起動経路（`subprocess.run("clear")`やTextual UI）はargs構築時に遮断する

logger初期化は全format共通経路に集約されているため、`force_text_on_stderr`の1フラグだけで
MCP経路の`stdin/stdout`専有を守れる。

### `run_for_agent`の実装経路

内部で`argparse.Namespace`を構築し、`run_pipeline`を直接呼び出す。
`run(sys_args=[...])`経由でargparseに渡す案ではエラーメッセージのstderr出力制御が困難で、
MCPツール側でのエラー整形ができないため不採用。
外部プロセス起動（`subprocess.run(["pyfltr", "run-for-agent", ...])`）案も検討した。
プロセス管理・`PYFLTR_CACHE_DIR`伝搬・`TERM`シグナル・テスト安定性の面で同一プロセス方式より不利のため不採用。

### `run_pipeline()`戻り値

`run_pipeline()`の戻り値は`(exit_code, run_id_or_None)`の2要素タプルとする。
2要素目はアーカイブ無効時・early exit時に`None`、それ以外では採番済みULIDが入る。

`only_failed`有効時に「直前runなし」「失敗ツールなし」「対象ファイル交差が空」のいずれかに該当した場合、
`run_pipeline`はearly exit（`(0, None)`）を返す。
このとき`run_for_agent`はエラーではなく「実行スキップ」（`skipped_reason`に理由文字列）を返す。

戻り値変更を採用したのは並行プロセス対策。
MCPツール側で`ArchiveStore.list_runs(limit=1)`を引く案では、同一ユーザーキャッシュを参照する
並行プロセスがあると別runの`run_id`を誤って拾うリスクがあるため戻り値経由とした。
