# JSONL出力の設計

LLMエージェントとCIシステム向けに提供するJSONL出力（およびSARIF/GitHub Annotation）の設計判断と内部仕様。
利用者向けのレコード書式は[CLIコマンド](../guide/usage.md#jsonl)を参照。

## レコード5種類

| `kind` | 役割 |
| --- | --- |
| `header` | 先頭1行。実行環境情報（`version`/`run_id`/`python`/`platform`/`cwd`/`commands`/`files`/`schema_hints`） |
| `warning` | pyfltrが検出した設定・実行時の警告（`source`で発生元を識別） |
| `diagnostic` | `(tool, file)`単位で集約された診断。個別指摘は`messages[]`配列に格納 |
| `tool` | 1ツール1レコードの実行メタ情報。rule→URL辞書`hint-urls`をオプションで含む |
| `summary` | 最終1行。全体集計。`failed > 0`のとき`guidance`配列を付与 |

stdout / `--output-file` のどちらでもストリーミング形式に統一する。
出力順は共通で、`header`を冒頭、ツール完了順に`diagnostic`+`tool`、末尾に`warning`+`summary`を出す。
旧「ファイル出力時は定義順バッチ」仕様は廃止した。
順序仕様は利用者向けにも公開しているため変更時は[CLIコマンド](../guide/usage.md#jsonl)も同時に更新する。

stdoutモードは`2>&1`マージ時のJSONL完全性をbest-effortとする。
`--output-format=jsonl`かつ`--output-file`未指定時はtext整形出力をstderrのWARN以上に抑止するため、
通常は混入しない。consumer側は`{`始まり行のみをJSONLとみなす実装にしておくことで、
stderr由来の警告が紛れ込んだ場合でも壊れにくい。

## LLM向けガイダンス

JSONLはLLMエージェントが入力として読むケースが多いため、フィールドの意味と失敗時の次アクションを英語で明示的に同梱する。
英語にするのはトークン効率（日本語より短くなりやすい）と汎用性（LLMの入力として標準的）のため。

- `header.schema_hints`: 毎runに付与する英語の辞書。`diagnostic.messages` / `diagnostic.messages.fix` /
  `diagnostic.messages.severity` / `tool.hint-urls` / `tool.retry_command` / `tool.cached` /
  `tool.truncated` / `header.run_id` の意味を短い英文で説明する。実装は`pyfltr/llm_output.py`の`_SCHEMA_HINTS`
- `summary.guidance`: `failed > 0`のときのみ付与する英語の配列。`tool.retry_command`の参照、
  `pyfltr run-for-agent --only-failed`、`diagnostic.fix`の解釈、`pyfltr show-run <run_id>`の案内を並べる。
  成功時（`failed == 0`）は省略する。各コマンド表記には起動時のlauncher（`pyfltr`／`uv run pyfltr`／`uvx pyfltr`）と
  実run_idが埋め込まれる。実装は`pyfltr/llm_output.py`の`_build_failure_guidance`

## diagnostic集約構造

`diagnostic`レコードは`(tool, file)`単位で1行にまとめる。個別指摘は`messages[]`配列に並び、
`{"line", "col", "rule", "severity", "fix", "msg"}`のサブセットを各要素が持つ（任意フィールドは該当値があるときのみ）。

集約処理は`pyfltr/llm_output.py`の`aggregate_diagnostics()`で行う。
`ErrorLocation`配列は`sort_errors()`で事前ソートされるが、
`messages[]`の並びは集約器内部で明示的に`(line, col or 0, rule or "")`の昇順に並べ替える。
ruleキーを含めることで、同一`(file, line, col)`内の重複指摘でも安定した順序を保つ。

同一ruleに対して異なる`rule_url`が紛れた場合、先に出現した値を採用して以降はwarningログに残す
（`llm_output.py`の`logger.warning`）。

## hint-urls 集約

ルールドキュメントURLは`diagnostic`本体に含めず、`tool`レコード末尾の`hint-urls`辞書
（ハイフン区切りキー）に集約する。

- キーはrule ID、値はURL。URLを生成できたruleのみ含める
- 同一ruleが複数出現してもURLは1つに束ねられる
- 1件も無ければ`hint-urls`フィールドごと省略する

外部に出るキー名は常に`hint-urls`（ハイフン）で統一する。Python / Pydantic内部では`hint_urls`
（アンダースコア）で扱い、`mcp_.py`の`ToolDiagnosticsModel`はPydanticのaliasで吸収する。
永続化先（`tools/<tool>/tool.json`）も`hint-urls`キーを使う。

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
集約過程で`tool.hint-urls`辞書にツール単位で束ねる。
対応ツールとURL組み立て方針は次の通り。

| ツール | URLテンプレート |
| --- | --- |
| `ruff-check` | JSON出力の`url`フィールドを最優先。無い場合は`https://docs.astral.sh/ruff/rules/{code}/` |
| `pylint` | `https://pylint.readthedocs.io/en/stable/user_guide/messages/{category}/{symbol}.html` |
| `pyright` | `https://microsoft.github.io/pyright/#/configuration?id={rule}` |
| `mypy` | `https://mypy.readthedocs.io/en/stable/_refs.html#code-{rule}` |
| `shellcheck` | `https://www.shellcheck.net/wiki/{rule}`（`rule`は`SC2086`形式） |
| `eslint` | `https://eslint.org/docs/latest/rules/{rule}`（プラグインルール`plugin/rule`はURL無し） |
| `markdownlint` | `https://github.com/DavidAnson/markdownlint/blob/main/doc/{rule}.md` |
| `textlint` | プラグイン間でURL体系が統一されていないため未対応 |

実装は`pyfltr/rule_urls.py`のテンプレート関数群を辞書ディスパッチで呼び分ける。
各カスタムパーサーは`build_rule_url(command=..., rule=..., existing_url=..., category=...)`を呼び、
`ErrorLocation.rule_url`に格納する。
ビルトイン正規表現経路でも、mypyの末尾`[error-code]`とmarkdownlintの先頭`MDxxx`を名前付きグループ`rule`で抽出し、
同じ関数でURLを補完する。`aggregate_diagnostics()`がそれらを舐めてツール単位の`hint-urls`辞書に統合する。

pylintは`rule`に`symbol`（`missing-module-docstring`等）を格納し、`message`に`messageId`を前置して
`"C0114: Missing module docstring"`の形で保持する。
`category`はpylint JSONの`type`フィールド（`convention`/`refactor`/`warning`/`error`/`fatal`/`information`）から決定する。

## retry_command

`retry_command`は当該ツール1件を再実行するshellコマンド文字列で、`tool`レコードに埋め込む。
`pyfltr/main.py`が`run_pipeline`呼び出し時点で次を構築する。

- 起動プレフィックス: Linuxでは`/proc/self/status`経由で親プロセスを辿り決定する。
  例えば`uv run`経由なら`uv run pyfltr`、`uvx`経由なら`uvx pyfltr`を採用する。
  macOSやWindowsなど親プロセスを取得できない環境では`sys.argv[0]`のbasenameにフォールバックする
- ベーステンプレート: 起動時の`sys.argv[1:]`をコピーして保持する。
  `--commands`値を当該ツールへ差し替え、位置引数（ターゲット）は除去する
- ターゲット: 当該`CommandResult.target_files`を絶対パス化して末尾に追加する。
  `--work-dir`適用前の元cwdを基準とすることで、再実行時のcwd二重解釈を避ける

このため`pyfltr ci`失敗時の`retry_command`に`pyfltr run`が混入してfixステージが暴発することは無い。
`--no-fix`/`--output-format`/`--output-file`/`--exit-zero-*`/`--exclude`などの実行意味論フラグも保持される。

`pass-filenames=False`のツールでは`commandline`にファイル引数が含まれない（tsc・cargo系・dotnet系等）。
このため`CommandResult`に`target_files: list[pathlib.Path]`フィールドを追加し、
`execute_command()`がツール実行時のターゲットリストをそのまま埋める。

### ターゲットを失敗ファイルに絞り込む

`retry_command`のターゲット位置引数は当該ツールで失敗したファイル
（`CommandResult.errors`の`ErrorLocation.file`集合）のみに絞る。

- 抽出元は`CommandResult.errors`。`target_files`との交差を並び順を保って返す（`pyfltr/main.py`の`_filter_failed_files()`）
- 失敗ファイル集合が空（`ErrorLocation.file`が取得できない・全体失敗のみ）の場合はターゲット位置引数を空のまま出力する
- キャッシュ復元結果（`cached=True`）では`retry_command`を埋めない（`retry_command=None`のまま）

絞り込み結果が空のときに`retry_command`を省略する代替案も検討したが、
pytestのように全体指定で意味を持つツールがあるため、空ターゲットで出力する方針を採った。
LLMが当該ツールを再実行する際、ターゲットを自分で補えば`pytest`をそのまま走らせられる。

## smart truncation

JSONL側で次の上限を適用する。`pyproject.toml`で調整可能。

| 設定キー | 既定値 | 意味 |
| --- | --- | --- |
| `jsonl-diagnostic-limit` | `0`（無制限） | 1ツールあたりの出力上限（集約前の個別指摘の合計で判定） |
| `jsonl-message-max-lines` | `30` | `tool.message`（生出力末尾）の行数上限 |
| `jsonl-message-max-chars` | `2000` | `tool.message`の文字数上限 |

diagnostic件数の切り詰めは集約前の`ErrorLocation`列を先頭N件で切ってから`aggregate_diagnostics()`に渡す順序で行う。
切り詰め後に`(tool, file)`集約されるため、結果の`diagnostic`行数と`messages[]`合計件数は切り詰め後の分布に依存する。

切り詰めが発生した場合は`tool`レコードに`truncated`サブオブジェクトを添付する。

- `diagnostics_total`: 切り詰め前の個別指摘の総件数（diagnostic切り詰め時のみ）
- `lines`: 切り詰め前の行数（メッセージ切り詰め時のみ）
- `chars`: 切り詰め前の文字数（メッセージ切り詰め時のみ）
- `archive`: 全文の参照パス（`tools/<tool>/output.log`または`tools/<tool>/diagnostics.jsonl`）

`messages[]`内に明示的なtruncationマーカーは置かず、`tool.truncated.diagnostics_total`と
集約後の`messages[]`合計件数との差分から推定する。

切り詰めの可否は`CommandResult.archived`フラグで判定する。
`archived=True`（アーカイブ書き込み成功）のときのみ切り詰めを適用し、`archived=False`では切り詰めをスキップして
全文をJSONLに出力する（復元不能な情報欠落の防止）。
`archived=False`になる具体例はアーカイブ無効・初期化失敗・個別ツールの書き込み失敗など。

判定単位はfixステージと通常ステージを区別する必要があるためCommandResult単位とする。
`_archive_hook()`が`write_tool_result()`成功時に`CommandResult.archived = True`を立てる。

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

## 出力形式とloggerの役割分担

pyfltrは3系統のloggerを使い分ける。

| logger名 | 用途 | 出力先の切替 |
| --- | --- | --- |
| root（既定） | system logger。設定エラー・アーカイブ初期化失敗などシステム診断 | 常にstderr。抑止しない |
| `pyfltr.textout` | 人間向けテキスト出力（進捗・詳細・summary・warnings・`--only-failed`案内） | format別にstream/level切替 |
| `pyfltr.structured` | 構造化出力（JSONL / SARIF） | StreamHandler(stdout) または FileHandler(`--output-file`) |

`pyfltr.textout`のformat別振る舞い（`pyfltr.cli.configure_text_output`で設定）。

| `output_format` | `output_file` | text stream | text level |
| --- | --- | --- | --- |
| `text`（既定） | 任意 | stdout | INFO |
| `github-annotations` | 任意 | stdout | INFO |
| `jsonl` | 未指定 | stderr | WARN |
| `jsonl` | 指定 | stdout | INFO |
| `sarif` | 未指定 | stderr | INFO |
| `sarif` | 指定 | stdout | INFO |
| 任意 | 任意（MCP経路） | stderr | INFO |

`pyfltr.structured`のhandler設定（`pyfltr.cli.configure_structured_output`で設定）。

- `jsonl` / `sarif` + `--output-file`未指定 → `StreamHandler(sys.stdout)`
- `jsonl` / `sarif` + `--output-file`指定 → `FileHandler(output_file, mode="w", encoding="utf-8")`
- `text` / `github-annotations` → handler未設定（構造化出力は発生しない）

stdout占有が起きるのは`jsonl` / `sarif`かつ`output_file`未指定時のみ。
`github-annotations`はtextと同じレイアウトを基本とし、エラー箇所だけGAワークフローコマンド記法
（`::error file=...::file:line:col: [tool:rule] message`）へ差し替える。
ログビューアがプロパティを剥がしても生ログ上でfile/line/ruleが読める契約にするため、
メッセージ本体にプレーンテキストのプレフィックスを埋め込む。

MCP経路（`pyfltr.mcp_.run_for_agent`）は同一プロセス内で`run_pipeline(..., force_text_on_stderr=True)`
を直接呼び、text_loggerをstderrに強制する。構造化出力は一時ファイル経由（FileHandler）となり
stdoutを汚染しない。
