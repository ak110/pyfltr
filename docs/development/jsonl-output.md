# JSONL出力の設計

LLMエージェントとCIシステム向けに提供するJSONL出力（およびSARIF/GitHub Annotation）の設計判断と内部仕様。
利用者向けのレコード書式は[CLIコマンド](../guide/usage.md#jsonl)を参照。

## レコード5種類

| `kind` | 役割 |
| --- | --- |
| `header` | 先頭1行。実行環境情報。`commands`は実行対象（有効化された）コマンド名配列。`schema_hints`は既定で短縮版、`-v`指定時にフル版へ切り替わる |
| `warning` | pyfltrが検出した設定・実行時の警告（`source`で発生元を識別） |
| `diagnostic` | `(command, file)`単位で集約された診断。個別指摘は`messages[]`配列に格納 |
| `command` | 1コマンド1レコードの実行メタ情報。rule→URL辞書`hint-urls`をオプションで含む |
| `summary` | 最終1行。全体集計。`no_issues` / `needs_action`の2グループに集計カウンタをネストする。`needs_action`配下の合計が1以上のとき`guidance`配列を付与 |

stdout / `--output-file` のどちらでもストリーミング形式に統一する。
出力順は共通で、`header`を冒頭、コマンド完了順に`diagnostic`+`command`、末尾に`warning`+`summary`を出す。
旧「ファイル出力時は定義順バッチ」仕様は廃止した。
順序仕様は利用者向けにも公開しているため変更時は[CLIコマンド](../guide/usage.md#jsonl)も同時に更新する。

stdoutモードは`2>&1`マージ時のJSONL完全性をbest-effortとする。
`--output-format=jsonl`かつ`--output-file`未指定時はtext整形出力をstderrのWARN以上に抑止するため、
通常は混入しない。consumer側は`{`始まり行のみをJSONLとみなす実装にしておくことで、
stderr由来の警告が紛れ込んだ場合でも壊れにくい。

## LLM向けガイダンス

JSONLはLLMエージェントが入力として読むケースが多いため、フィールドの意味と失敗時の次アクションを英語で明示的に同梱する。
英語にするのはトークン効率（日本語より短くなりやすい）と汎用性（LLMの入力として標準的）のため。

- `header.schema_hints`: 毎runに付与する英語の辞書。
  既定はLLMが読み違いやすい非自明フィールドのみに絞った短縮版、`-v` / `--verbose`指定時にフィールド単位の詳細を含むフル版に切り替わる。
  各版の本文と文字数閾値は`pyfltr/llm_output.py`を参照。公開窓口は同モジュールの`get_schema_hints(full=...)`
- `summary.applied_fixes`: fixステージ・formatterステージで実際にファイル内容が変化した対象のパス一覧（ソート済み）。
  全コマンドのfixed_filesをユニオンして集計する。変化がなかった場合は省略する。
  書式詳細は[利用者向けガイド](../guide/usage.md)を参照
- `summary.guidance`: `failed + resolution_failed > 0`のときのみ付与する英語の配列。`command.retry_command`の参照、
  `pyfltr run-for-agent --only-failed`、`diagnostic.fix`の解釈、`pyfltr show-run <run_id>`の案内を並べる。
  成功時は省略する。各コマンド表記には起動時のlauncher（`pyfltr`／`uv run pyfltr`／`uvx pyfltr`）と実run_idが埋め込まれる

## diagnostic集約構造

`diagnostic`レコードは`(command, file)`単位で1行にまとめる。個別指摘は`messages[]`配列に並び、
`{"line", "col", "rule", "severity", "fix", "msg"}`のサブセットを各要素が持つ（任意フィールドは該当値があるときのみ）。

集約処理は`pyfltr/llm_output.py`で行う。
`ErrorLocation`配列は事前ソートされるが、`messages[]`の並びは集約器内部で
`(line, col or 0, rule or "")`の昇順に並べ替える。
ruleキーを含めることで、同一`(file, line, col)`内の重複指摘でも安定した順序を保つ。

同一ruleに対して異なる`rule_url`が紛れた場合、先に出現した値を採用してwarningログに残す。

## hint-urls 集約

ルールドキュメントURLは`diagnostic`本体に含めず、`command`レコード末尾の`hint-urls`辞書
（ハイフン区切りキー）に集約する。

- キーはrule ID、値はURL。URLを生成できたruleのみ含める
- 同一ruleが複数出現してもURLは1つに束ねられる
- 1件も無ければ`hint-urls`フィールドごと省略する

外部に出るキー名は常に`hint-urls`（ハイフン）で統一する。Python / Pydantic内部では`hint_urls`
（アンダースコア）で扱い、`mcp_.py`の`ToolDiagnosticsModel`はPydanticのaliasで吸収する。
永続化先（`tools/<sanitize(command)>/tool.json`）も`hint-urls`キーを使う。

## diagnostic.messages.fix の値域

`diagnostic.messages[].fix`は次の4値を取り、値なし（フィールドごと省略）を加えて5状態を表現する。

| 値 | 意味 |
| --- | --- |
| `"safe"` | ツールが安全と判断する自動修正を提示している |
| `"unsafe"` | 自動修正可能だが副作用の可能性があるとツールが判断している（ruff`--unsafe-fixes`等） |
| `"suggested"` | ツールが候補を示しているが適用は人間の判断に委ねる |
| `"none"` | ツールが自動修正情報を返した上で「自動修正不可」と明示している |
| 省略（`None`） | ツールが自動修正情報を返さない（regex パース系の mypy / pylint / markdownlint など） |

ruff-check / shellcheck / textlint / eslint / typosは自動修正情報の有無をJSON出力で明示する。
パーサーは「情報が来たが候補ゼロ」を`"none"`として明示し、パーサー自体が情報を返さない場合は`None`として省略する。

## severityの3値統一

`diagnostic.messages[].severity`は`"error"`/`"warning"`/`"info"`のいずれかに正規化する。
各ツールの生値は`error_parser._normalize_severity()`で3値にマップする
（mypyの`error`、pylintの`convention`、shellcheckの`STYLE`等）。
未対応値は`None`としてフィールドごと省略する。
severityを持たないツール（mypy・markdownlint・ty等）は従来どおり`None`を維持する。

## rule_url（ツール単位の hint-urls へ集約）

`ErrorLocation.rule_url`は当該ruleの公式ドキュメントURLを指す文字列で、
集約過程で`command.hint-urls`辞書にツール単位で束ねる。
対応ツールとURLテンプレートは`pyfltr/rule_urls.py`を参照。
ruff / pylint / pyright / mypy / shellcheck / eslint / markdownlintをカバーする。
textlintはプラグイン間でURL体系が統一されていないため未対応。

ビルトイン正規表現経路では、mypyの末尾`[error-code]`とmarkdownlintの先頭`MDxxx`を名前付きグループ`rule`で抽出し、
同じテンプレート関数でURLを補完する。集約器がそれらを舐めてツール単位の`hint-urls`辞書に統合する。

pylintは`rule`に`symbol`（`missing-module-docstring`等）を格納し、`message`に`messageId`を前置して
`"C0114: Missing module docstring"`の形で保持する。
`category`はpylint JSONの`type`フィールド（`convention`/`refactor`/`warning`/`error`/`fatal`/`information`）から決定する。

## retry_command

`retry_command`は当該ツール1件を再実行するshellコマンド文字列で、`command`レコードに埋め込む。
構築の構成要素は次の3点。

- 起動プレフィックス: 親プロセスから`uv run pyfltr`/`uvx pyfltr`/`pyfltr`を判定する。
  Linuxでは`/proc/self/status`経由、macOS/Windowsではargv basenameへフォールバックする
- ベーステンプレート: 起動時のargvをコピーし、`--commands`値を当該ツールへ差し替え、位置引数を除去する
- ターゲット: 当該ツールで失敗したファイルを絶対パス化して末尾に追加する。
  `--work-dir`適用前の元cwdを基準とすることで、再実行時のcwd二重解釈を避ける

このため`pyfltr ci`失敗時の`retry_command`に`pyfltr run`が混入してfixステージが暴発することは無い。
`--no-fix`/`--output-format`/`--output-file`/`--exit-zero-*`/`--exclude`などの実行意味論フラグも保持される。

`pass-filenames=False`のツールでは`commandline`にファイル引数が含まれない（tsc・cargo系・dotnet系等）。
このため`CommandResult`はツール実行時のターゲットリストを別フィールドで保持する。

### ターゲットを失敗ファイルに絞り込む

`retry_command`のターゲット位置引数は当該ツールで失敗したファイルのみに絞る。
抽出ロジックは`pyfltr/retry.py`の`filter_failed_files`に集約する。

- 失敗ファイル集合が空（`ErrorLocation.file`が取得できない・全体失敗のみ）の場合はターゲット位置引数を空のまま出力する
- キャッシュ復元結果（`cached=True`）では`retry_command`を埋めない

絞り込み結果が空のときに`retry_command`を省略する代替案も検討したが、
pytestのように全体指定で意味を持つツールがあるため、空ターゲットで出力する方針を採った。
LLMが当該ツールを再実行する際、ターゲットを自分で補えば`pytest`をそのまま走らせられる。

## smart truncation

JSONL側で次の上限を適用する（`pyproject.toml`で調整可能）。

| 設定キー | 既定値 | 意味 |
| --- | --- | --- |
| `jsonl-diagnostic-limit` | `0`（無制限） | 1ツールあたりの出力上限（集約前の個別指摘の合計で判定） |
| `jsonl-message-max-lines` | `30` | `command.message`（生出力末尾）の行数上限 |
| `jsonl-message-max-chars` | `2000` | `command.message`の文字数上限 |

diagnostic件数の切り詰めは集約前の`ErrorLocation`列を先頭N件で切ってから集約処理に渡す。
切り詰め後に`(command, file)`集約されるため、結果の`diagnostic`行数と`messages[]`合計件数は切り詰め後の分布に依存する。

切り詰めが発生した場合は`command`レコードに`truncated`サブオブジェクトを添付する。
詳細フィールド（`diagnostics_total` / `lines` / `chars` / `head_chars` / `tail_chars` / `archive`）は
[利用者向けJSONLスキーマ](../guide/usage.md#jsonl)を参照。

`command.message`の切り詰めはハイブリッド方式で行う。
書式は「先頭ブロック + 中略マーカー `\n... (truncated)\n` + 末尾ブロック」。
冒頭にエラー要約を出すツール（editorconfig-checker等）と、末尾にスタックトレースを出すツール（pytest・mypy等）の双方を救う狙いがある。
`jsonl-message-max-chars`を先頭・末尾に按分する比率は`pyfltr/llm_output.py`を参照。
`jsonl-message-max-lines`は末尾側のみに適用する（先頭側は文字数制限で十分に絞られるため）。

`messages[]`内に明示的なtruncationマーカーは置かず、`command.truncated.diagnostics_total`と
集約後の`messages[]`合計件数との差分から推定する。

切り詰めの可否はアーカイブ書き込み成功フラグで判定する。
書き込み成功時のみ切り詰めを適用し、失敗時は全文をJSONLに出力する（復元不能な情報欠落の防止）。
fixステージと通常ステージを区別する必要があるため、判定単位はステージごとのCommandResult単位とする。

## SARIF 2.1.0出力

`--output-format=sarif`でSARIF 2.1.0準拠のJSONをstdout（または`--output-file`）へ書き出す。
1ツール = 1 runオブジェクトとして対応付け、`tool.driver.rules`に重複なしで`rule`と`helpUri`を登録し、
`results`配列にdiagnosticを配置する。

severityからSARIF levelへの変換。

- `error` → `"error"`
- `warning` → `"warning"`
- `info` → `"note"`
- 未設定 → `"warning"`（フォールバック）

`retry_command`は`invocations[].commandLine`に、`run_id`/`exit_code`/`commands`/`files`は`properties.pyfltr`に格納する。

## GitHub Annotation出力

`--output-format=github-annotations`でGitHub Actionsの注釈形式（`::error file=...`）を出力する。
GitHub Actionsが拾ってプル要求のファイル行にインライン表示する用途。

severity → ディレクティブのマップ。

- `error` → `::error`
- `warning` → `::warning`
- `info` → `::notice`
- 未設定 → `::warning`

`file`/`line`/`col`/`title`をプロパティとして付与し、本文はGitHub仕様に沿って`%`/改行をパーセントエンコードする。
`title`は`{tool}: {rule}`形式でruleが無ければtool名のみを使う。

## Code Quality出力

`--output-format=code-quality`でCode Climate JSON issue形式のサブセット（JSON配列）を
stdout（または`--output-file`）へ書き出す。
GitLab CIの`artifacts:reports:codequality`取り込みを想定した形式。

severityからCode Quality severityへのマップ。Code Quality仕様は`info` / `minor` / `major` / `critical` / `blocker`の
5段階だが、pyfltr側に対応情報が無く過大評価を避けるため上位2段階は使わない。

- `error` → `"major"`
- `warning` → `"minor"`
- `info` → `"info"`
- 未設定 → `"minor"`（フォールバック）

`check_name`は`{tool}:{rule}`形式（ruleが無ければtool名のみ）。
`location.path`はpyfltr内部で相対パス保持済みのためそのまま使う。
`location.lines.begin`は`message.line`がNoneまたは0のとき1に補正する（Code Qualityは0行を許容しないため）。

`fingerprint`はtool・file・line・col・rule・msgをタブ区切りで連結した文字列のSHA-256全桁を採用する。
同一指摘の重複統合に足るユニーク性を確保しつつ、配置順の変化に頑強にする。

書き出しは構造化出力logger（`pyfltr.structured`）経由の1回のみで、JSONLのようなstreamingは行わない。

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。

| logger名 | 用途 | 出力先の切替 |
| --- | --- | --- |
| root（既定） | system logger。設定エラー・アーカイブ初期化失敗などシステム診断 | 常にstderr。抑止しない |
| `pyfltr.textout` | 人間向けテキスト出力（進捗・詳細・summary・warnings・`--only-failed`案内） | format別にstream/level切替 |
| `pyfltr.structured` | 構造化出力（JSONL / SARIF / Code Quality） | StreamHandler(stdout) または FileHandler(`--output-file`) |

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

stdout占有が起きるのは`jsonl` / `sarif` / `code-quality`かつ`output_file`未指定時のみ。
`github-annotations`はtextと同じレイアウトを基本とし、エラー箇所だけGAワークフローコマンド記法
（`::error file=...::file:line:col: [tool:rule] message`）へ差し替える。
ログビューアがプロパティを剥がしても生ログ上でfile/line/ruleが読める契約にするため、
メッセージ本体にプレーンテキストのプレフィックスを埋め込む。

MCP経路（`pyfltr.mcp_.run_for_agent`）は同一プロセス内で`run_pipeline(..., force_text_on_stderr=True)`
を直接呼び、text_loggerをstderrに強制する。構造化出力は一時ファイル経由（FileHandler）となり
stdoutを汚染しない。
