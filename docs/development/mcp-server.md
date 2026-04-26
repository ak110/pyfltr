# MCPサーバー

`pyfltr mcp`サブコマンドが提供するMCP（Model Context Protocol）サーバーの設計判断と内部仕様。
利用者向けの起動方法・MCPツール一覧・MCPクライアント設定例は[CLIコマンド](../guide/usage.md)を参照。

## 提供ツール

読み取り系4ツール・実行系1ツールの計5ツールを公開する。

| ツール名 | 対応CLI | 主要引数 |
| --- | --- | --- |
| `list_runs` | `pyfltr list-runs` | `limit: int = 20` |
| `show_run` | `pyfltr show-run <run_id>` | `run_id: str`（前方一致・`latest`可） |
| `show_run_diagnostics` | `show-run <run_id> --commands <name>` | `run_id`・`commands: list[str]` |
| `show_run_output` | `show-run <run_id> --commands <name> --output` | `run_id: str`・`commands: list[str]` |
| `run_for_agent` | `pyfltr run-for-agent` | `paths`・`commands`など5引数（詳細は下記） |

戻り値:

- `list_runs`: `RunSummary[]`（run_id・開始終了時刻・exit_code等）
- `show_run`: `{meta: dict, commands: CommandSummary[]}`
- `show_run_diagnostics`: `[{command_meta, diagnostics[]}, ...]`（入力 `commands` の順）
- `show_run_output`: `dict[str, str]`（コマンド名→全文）
- `run_for_agent`: `RunForAgentResult`

`run_for_agent`はCLIの`run-for-agent`サブコマンド相当の前提
（`--output-format=jsonl`既定、fixステージ有効、formatterの書き換えは成功扱い）で動作する。

ツール名はCLIサブコマンドのハイフン形式と異なりアンダースコア形式（`list_runs`/`show_run`等）とする。
ハイフンはPythonの`@mcp.tool()`名として非推奨のため。

## 実装配置

新規モジュール`pyfltr/mcp_.py`に全MCPツール実装と`register_subparsers()`を置く。
`runs.py`と同じ「サブコマンド1つ＋独自出力経路」の粒度で責務を明確化する。

`main.py`は既存の`pyfltr.runs.register_subparsers()`と同じパターンで`pyfltr.mcp_.register_subparsers()`を呼び出すだけに留める。
`mcp_.py`のサフィックス付き命名は`warnings_.py`と同じ方針で、サードパーティ`mcp`パッケージとのimport衝突事故を予防する。

## MCPライブラリの選定

`mcp.server.fastmcp.FastMCP`を採用する。

- MCP公式SDKのREADMEで推奨される高レベルDSLで記述量が最小
- 型ヒントからinputSchemaとoutputSchemaを自動生成でき、Pydantic BaseModelとの親和性が高い
- stdioトランスポート起動が`mcp.run(transport="stdio")`の一行で済み、asyncio・`stdio_server()`の明示的な管理が不要
- 高度なnotificationや動的capability交渉を必要とせず、low-level API（`mcp.server.Server`）の利点が得られない

## 戻り値のPydantic化

Pydantic BaseModel派生の6クラスを`mcp_.py`内で定義する。

- `RunSummaryModel`
- `CommandSummaryModel`
- `DiagnosticModel`
- `RunOverviewModel`
- `CommandDiagnosticsModel`
- `RunForAgentResult`

理由は次の通り。

- FastMCPはBaseModelの`Field(description=...)`をMCPスキーマへ自動反映し、LLMエージェント側で引数・戻り値の意味を把握しやすい
- 既存の`archive.ArchiveStore`が返す`dict[str, Any]`をBaseModelに一度通すことで必須フィールドの型保証が得られる
- JSONLスキーマ（`run_id`・`rule_url`・`retry_command`等）のフィールド名と揃え、CLI側の出力との認知負荷差を減らす

## stdio隔離

stdioトランスポートはstdin/stdoutをJSON-RPCフレームに専有するため、
どの経路であれstdoutへの書き込みはプロトコル破壊を引き起こす。
3層で隔離を実施する。

1. `pyfltr mcp`起動直後に`logging.basicConfig(stream=sys.stderr, ...)`を強制し、root loggerの出力先をstderrへ向ける
2. `run_for_agent`ツール内で`run_pipeline(..., force_text_on_stderr=True)`を呼び、
   人間向けtext整形loggerを強制的にstderrへ向ける。
   構造化出力は一時ファイルを`--output-file`として渡すため`FileHandler`経由でファイルに流れる
3. `args.no_ui = True` / `args.no_clear = True` / `args.stream = False`を`args`構築時に設定し、
   `subprocess.run("clear")`やTUIの起動経路を遮断する

`run_pipeline`内の`_configure_loggers_for_format`が全format共通の初期化経路で、
`force_text_on_stderr=True`を渡せばtext_loggerは常にstderrに向かう。
MCP経路が増えてもこの1点だけで`stdin/stdout`専有を守れる。

## `run_for_agent`の実装経路

内部で`argparse.Namespace`を構築し、`run_pipeline(args, commands, config)`を直接呼び出す。

- 引数は`paths`/`commands`/`fail_fast`/`only_failed`/`from_run`の5つをMCPクライアントから受け付ける
- `only_failed`・`from_run`はCLIの`--only-failed`/`--from-run`と同等のセマンティクスで動作し、
  `pyfltr.only_failed.apply_filter`経路を再利用する
- `from_run` 単独指定（`only_failed=False` かつ `from_run` 指定）は`ValueError`を送出する（CLIと同等）
- 他のフラグ（`no-archive`/`no-cache`/`config`/`output-format`）は固定とする。
  archive・cacheは有効、設定はCWDの`pyproject.toml`を使用、`output-format=jsonl`の出力先は一時ファイルとする
- `commands`は既存`--commands`と同じセマンティクス（カンマ区切り文字列ではなくツール名リスト）で受け取る
- `run_pipeline`は`force_text_on_stderr=True`付きで呼び、text整形loggerをstderrに強制する

### `schema_hints` / `retry_commands` の付加

戻り値`RunForAgentResult`に`schema_hints`と`retry_commands`フィールドを追加し、
MCPクライアント（コーディングエージェント）が結果を解釈するのに必要な情報を1回のレスポンスで完結させる。

- `schema_hints`: `pyfltr.llm_output.get_schema_hints(full=False)`の短縮版を埋め込む。
  LLMがJSONL出力の各フィールドの意味を把握できるようにする
- `retry_commands`: 失敗コマンドのみをキー化し、アーカイブの`tool.json`から`retry_command`を読み込んで辞書化する。
  成功・cachedのコマンドはキーごと省略する

### 設計判断

`run(sys_args=[...])`経由でargparseに渡すとエラーメッセージがstderrへ書かれる制御が困難で、
MCPツール側でのエラー整形ができない。
`argparse.Namespace`直接構築なら引数検証をMCPツール側（Pydantic）に任せられる。

`subprocess.run(["pyfltr", "run-for-agent", ...])`で外部プロセス起動する案も検討した。
stdio隔離が自然になる利点はあるが、FastMCPサーバーとlintプロセスが並行稼働する際の
プロセス管理・`PYFLTR_CACHE_DIR`伝搬・`TERM`シグナルハンドリング・テストの安定性で不利となるため不採用とした。
同一プロセス内で構造化出力を抑止する方が制御しやすい。

## `run_pipeline()`戻り値の拡張

`run_pipeline()`の戻り値を`tuple[int, str | None]`へ拡張する。
1要素目は`exit_code`、2要素目は採番済み`run_id`（アーカイブ無効時・early exit時は`None`）。

MCPツール側では`run_id is None`を「early exit」として解釈し、
`skipped_reason`フィールドを設定した戻り値を返す設計とする。
通常実行では実行アーカイブを強制有効化するため`run_id`は必ず採番される。

### early exit と戻り値設計

`only_failed`有効時に「直前runなし」「失敗ツールなし」「対象ファイル交差が空」の場合、
`run_pipeline`が`(0, None)`を返す（early exit）。
このとき`run_for_agent`はエラーではなく「実行スキップ」を表す戻り値を返す。

- `run_id`: `None`（実行が行われていないためULIDは採番されない）
- `exit_code`: `0`
- `failed`: `[]`
- `commands`: `[]`
- `skipped_reason`: スキップ理由の説明文字列

通常実行時（`only_failed=False` またはearly exitに至らなかった場合）は`run_id`を必ず含む。

戻り値変更を採用した代替案。

- `run_pipeline()`の戻り値は変えず、MCPツール側で`ArchiveStore.list_runs(limit=1)`を呼んで最新`run_id`を取得する案。
  同一ユーザーキャッシュを参照する並行プロセスが存在する場合に別runの`run_id`を誤って拾うリスクがある
- `run_pipeline()`に`on_run_id: Callable[[str], None]`コールバック引数を追加する案。
  呼び出し側で採番値を受け取るための同期補助が必要になり、`_run_impl`からの呼び出しでも
  余計な一時変数が増える。タプル戻り値の方が素直

## 依存

`mcp>=1.0`は`pyproject.toml`の`[project.dependencies]`のまま本体必須として扱う。
optional extras（`pyfltr[mcp]`）への分離はしない。

- 「pyfltrを入れれば即座にMCPとして使える」体験を崩さない
- `mcp`パッケージは`httpx`/`starlette`/`uvicorn`等を引き込むが、本体依存としての受容はユーザー合意済み

## 提供範囲を絞った代替案

- `pyfltr mcp --http`等で複数トランスポートを提供する案 —
  stdio特化のシンプルな起動形態に絞り、配布・運用・認証の設計判断を先送りしない
- 実行系ツールを`run`/`ci`/`fast`/`run-for-agent`の4サブコマンドすべて露出する案 —
  エージェント連携が想定用途のため`run-for-agent`相当1本に絞る
- `run_for_agent`で`no-archive`/`no-cache`/`config`/`output-format`をMCPパラメーターとして公開する案 —
  パラメーター増加分だけMCPスキーマが肥大化し、stdio隔離も複雑化する。
  `pyproject.toml`と環境変数で制御できる項目はCWD依存のままとし、MCPクライアント側の制御ポイントは最小化する
