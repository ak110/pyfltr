# MCPサーバー

`pyfltr mcp`サブコマンドが提供するMCP（Model Context Protocol）サーバーの設計判断と内部仕様。
利用者向けの起動方法・MCPツール一覧・MCPクライアント設定例は[CLIコマンド](../guide/usage.md)を参照。

## 提供ツール

読み取り系4ツール・実行系1ツールの計5ツールを公開する。
ツール名・引数・戻り値の詳細は`pyfltr/mcp_.py`の各`@mcp.tool()`定義を参照。

`run_for_agent`はCLIの`run-for-agent`サブコマンド相当の前提
（`--output-format=jsonl`既定、fixステージ有効、formatterの書き換えは成功扱い）で動作する。

実行系を`run-for-agent`相当1本に絞ったのは、エージェント連携用途では`ci`/`run`/`fast`の差分を露出する必要が薄く、
パラメーター数を抑えてMCPスキーマを単純化するため。

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

戻り値は`mcp_.py`内で定義したPydantic BaseModel派生クラス群でラップする。
クラス構成と各フィールドの意味は同モジュールを参照。

理由は次の通り。

- FastMCPはBaseModelの`Field(description=...)`をMCPスキーマへ自動反映し、LLMエージェント側で引数・戻り値の意味を把握しやすい
- `ArchiveStore`が返す`dict[str, Any]`をBaseModelに一度通すことで必須フィールドの型保証が得られる
- JSONLスキーマ（`run_id`・`rule_url`・`retry_command`等）のフィールド名と揃え、CLI側の出力との認知負荷差を減らす

## stdio隔離

stdioトランスポートはstdin/stdoutをJSON-RPCフレームに専有するため、
どの経路であれstdoutへの書き込みはプロトコル破壊を引き起こす。
3層で隔離を実施する。

1. 起動直後にroot loggerの出力先をstderrへ強制する
2. `run_for_agent`ツール内では`run_pipeline`に`force_text_on_stderr=True`を渡し、人間向けtext整形loggerをstderrへ向ける。
   構造化出力は一時ファイルへFileHandler経由で書き出す
3. TUI起動経路（`subprocess.run("clear")`やTextual UI）はargs構築時に遮断する

logger初期化は全format共通経路に集約されているため、`force_text_on_stderr`の1フラグだけで
MCP経路の`stdin/stdout`専有を守れる。

## `run_for_agent`の実装経路

内部で`argparse.Namespace`を構築し、`run_pipeline`を直接呼び出す。

- MCPクライアントから受け付ける引数はCLIの`--commands`/`--fail-fast`/`--only-failed`/`--from-run`相当
- `from_run`単独指定（`only_failed=False`かつ`from_run`指定）はCLIと同等にエラー扱い
- archive/cache/configは固定値、`output-format=jsonl`の出力先は一時ファイルとする
 （MCPクライアント側に制御ポイントを増やさず、stdio隔離も簡潔に保つため）
- 戻り値の`schema_hints`はJSONL各フィールドの意味解釈用の英語短縮版を埋め込む。
  `retry_commands`は失敗コマンド名→再実行shellコマンド辞書を入れ、成功・cachedは省略する

### 設計判断

`run(sys_args=[...])`経由でargparseに渡すとエラーメッセージがstderrへ書かれる制御が困難で、
MCPツール側でのエラー整形ができない。
`argparse.Namespace`直接構築なら引数検証をMCPツール側（Pydantic）に任せられる。

外部プロセス起動（`subprocess.run(["pyfltr", "run-for-agent", ...])`）案も検討した。
stdio隔離が自然になる利点はあるが、プロセス管理・`PYFLTR_CACHE_DIR`伝搬・`TERM`シグナル・テスト安定性の面で不利となるため不採用。
同一プロセス内で構造化出力を抑止する方が制御しやすい。

## `run_pipeline()`戻り値

`run_pipeline()`の戻り値は`(exit_code, run_id_or_None)`の2要素タプルとする。
2要素目はアーカイブ無効時・early exit時に`None`、それ以外では採番済みULIDが入る。
MCPツール側では`run_id is None`を「early exit」として解釈し、`skipped_reason`を設定した戻り値を返す。

### early exit

`only_failed`有効時に「直前runなし」「失敗ツールなし」「対象ファイル交差が空」のいずれかに該当した場合、
`run_pipeline`はearly exit（`(0, None)`）を返す。
このとき`run_for_agent`はエラーではなく「実行スキップ」（`skipped_reason`に理由文字列）を返す。

戻り値変更を採用した代替案。

- 戻り値は変えずMCPツール側で`ArchiveStore.list_runs(limit=1)`を引く案。
  同一ユーザーキャッシュを参照する並行プロセスがあると別runの`run_id`を誤って拾うリスクがある
- `on_run_id`コールバック引数を追加する案。
  同期補助が必要になり、内部呼び出し側でも余計な一時変数が増える。タプル戻り値のほうが素直

## 依存

`mcp>=1.0`は`pyproject.toml`の`[project.dependencies]`のまま本体必須として扱う。
optional extras（`pyfltr[mcp]`）への分離はしない。

- 「pyfltrを入れれば即座にMCPとして使える」体験を崩さない
- `mcp`パッケージは`httpx`/`starlette`/`uvicorn`等を引き込むが、本体依存としての受容はユーザー合意済み

## 却下した代替案

- 複数トランスポート（`pyfltr mcp --http`等）の提供 —
  stdio特化のシンプルな起動形態に絞り、配布・運用・認証の設計判断を先送りしない
- 実行系ツールの全サブコマンド露出（`run`/`ci`/`fast`/`run-for-agent`）—
  エージェント連携が想定用途のため`run-for-agent`相当1本に絞る
- `no-archive`/`no-cache`/`config`/`output-format`のMCPパラメーター化 —
  パラメーター増加分だけMCPスキーマが肥大化し、stdio隔離も複雑化する
