# パート F: MCPサーバー化

v3.0.0で追加する `pyfltr mcp` サブコマンドとMCPツール群の設計判断と仕様。
実行アーカイブ（パートB）と実行パイプライン（パートD）をMCPクライアントから利用可能にする。

## 恒常配置の対応先

v3.0.0全体で恒常配置（`docs/features/`・`docs/topics/`）は未整備のため、本パートも開発中配置のみで完結する。
v3リリース完了後に`spec-driven-promote`スキルで恒常配置を整備する際、本ドキュメントは機能ドキュメント（例: `docs/features/MCPサーバー.md`）へ昇格する。

## 目的

MCP (Model Context Protocol) クライアントを備えるLLMエージェントから、pyfltrの実行と実行アーカイブ参照を直接利用できるようにする。
既存の `pyfltr run-for-agent` / `pyfltr list-runs` / `pyfltr show-run` のCLIサブコマンドが提供する機能を、ツール呼び出し経由で等価に提供する。

## 提供サブコマンド

### mcp

stdioトランスポートでMCPサーバーを起動する。

主な要件:

- 引数なしで起動し、標準入出力をJSON-RPCフレームに専有する
- サーバー終了はstdin EOFで検知する（MCPクライアントが接続を閉じた時点）
- 追加の構成オプションは持たない（トランスポート固定・ツール固定）

## 提供MCPツール

読み取り系4ツール・実行系1ツールの計5ツールを提供する。

### 読み取り系

| ツール名 | 対応CLI | 主要引数 | 戻り値 |
| --- | --- | --- | --- |
| `list_runs` | `pyfltr list-runs` | `limit: int = 20` | `RunSummary[]`（`run_id`・`started_at`・`finished_at`・`exit_code`・`commands`・`files`） |
| `show_run` | `pyfltr show-run <run_id>` | `run_id: str`（前方一致・`latest`可） | `{meta: dict, tools: ToolSummary[]}` |
| `show_run_diagnostics` | `pyfltr show-run <run_id> --tool <name>` | `run_id: str`・`tool: str` | `{tool_meta: dict, diagnostics: dict[]}` |
| `show_run_output` | `pyfltr show-run <run_id> --tool <name> --output` | `run_id: str`・`tool: str` | `str`（`output.log`全文） |

### 実行系

| ツール名 | 対応CLI | 主要引数 | 戻り値 |
| --- | --- | --- | --- |
| `run_for_agent` | `pyfltr run-for-agent` | `paths: list[str]`・`commands: list[str] \| None = None`・`fail_fast: bool = False` | `{run_id: str, exit_code: int, failed: list[str], tools: ToolSummary[]}` |

`run_for_agent` はCLIの `run-for-agent` サブコマンド相当の前提（`--output-format=jsonl` 既定、fixステージ有効、formatterの書き換えは成功扱い）で動作する。

## スコープ

| 項目 | 内容 |
| --- | --- |
| 対象モジュール | `pyfltr/mcp_.py` (新規) ・ `pyfltr/main.py` (サブパーサー追加) |
| 既存基盤 | `pyfltr/archive.py` の読み取りAPI ・ `pyfltr/runs.py` の `_resolve_run_id()` ・ `pyfltr/main.py` の `run_pipeline()` |
| CLI追加 | `pyfltr mcp` |
| 設定キー追加 | なし（既存のアーカイブ・キャッシュ設定を利用） |
| MCPライブラリ | `mcp>=1.0` （本体必須依存、確定済み）・FastMCP (`mcp.server.fastmcp.FastMCP`) を採用 |
| 依存追加 | なし（`mcp>=1.0` は既に `pyproject.toml` に入っている） |

## 受け入れ基準

- `pyfltr mcp` でFastMCP製のMCPサーバーが起動し、stdin EOFで終了する
- `list_runs` で過去run一覧が新しい順で取得できる
- `show_run` で指定runのmetaと全ツールサマリが取得できる。前方一致・`latest`エイリアス・曖昧一致エラーはCLIの `show-run` と同挙動
- `show_run_diagnostics` で指定ツールの `tool.json` と `diagnostics.jsonl` 全件が取得できる
- `show_run_output` で指定ツールの `output.log` 全文を文字列として取得できる
- `run_for_agent` で指定パスに対するlint一式が走り、`run_id` と終了コード・失敗ツール名を取得できる
- MCPサーバー起動中、root loggerと `run_pipeline` 内部の構造化出力はすべてstdoutを汚染しない（JSON-RPCフレームのみがstdoutへ出る）
- 存在しない `run_id` ・ `tool` 指定はMCPプロトコルのエラー応答として返り、サーバーは稼働継続する
- `pyfltr run-for-agent` を外部から実行した結果と、MCP `run_for_agent` を呼んだ結果は `run_id` 採番以外同一のアーカイブ構造を持つ

## 主要設計判断

### MCPライブラリにFastMCPを採用する

- 決定内容: `mcp.server.fastmcp.FastMCP` を使い、`@mcp.tool()` デコレーターで5ツールを登録する
- 理由:
    - MCP公式SDKのREADMEで推奨される高レベルDSLで、記述量が最小
    - 型ヒントからinputSchemaとoutputSchemaを自動生成でき、Pydantic BaseModelとの親和性が高い
    - stdioトランスポート起動が `mcp.run(transport="stdio")` の一行で済み、asyncio・`stdio_server()` の明示的な管理が不要
    - 本パートでは高度なnotificationや動的capability交渉を必要とせず、low-level APIの利点が得られない

### 実装モジュールを `pyfltr/mcp_.py` に集約する

- 決定内容: 新規モジュール `pyfltr/mcp_.py` に全MCPツール実装と `register_subparsers()` を置く
- 理由:
    - パートEの `pyfltr/runs.py` と同じ「サブコマンド1つ＋独自出力経路」の粒度で、責務を明確化する
    - `main.py` は既存の `pyfltr.runs.register_subparsers()` と同じパターンで `pyfltr.mcp_.register_subparsers()` を呼び出すだけに留める
    - `mcp` はPython標準ライブラリの `mcp`（存在しない）と衝突しないが、pyfltr内部名として `mcp_.py` のサフィックス付き命名（`warnings_.py` と同じ）にすることで将来的な誤import事故を防ぐ

### stdio隔離は3層で実施する

- 決定内容:
  1. `pyfltr mcp` 起動直後に `logging.basicConfig(stream=sys.stderr, ...)` を強制し、root loggerの出力先をstderrへ向ける
  2. `run_for_agent` ツール内で `run_pipeline()` を呼ぶ前に `_force_structured_stdout_mode` / `_suppress_logging` 相当を適用し、構造化出力をファイルへ誘導する
  3. `subprocess.run("clear")` を抑止するため `args.no_clear = True` を強制する
- 理由:
    - stdioトランスポートはstdin/stdoutをJSON-RPCフレームに専有するため、どの経路であれstdoutへの書き込みはプロトコル破壊を引き起こす
    - パートC・D・Eで既に確立された抑止パターン（`_force_structured_stdout_mode` / `_suppress_logging`）を再利用する。MCP起動時・ツール呼び出し時の双方で適用する
    - TUI経路は `can_use_ui()` が `sys.stdout.isatty()` を判定するためMCPサーバー内では自動抑止されるが、`args.no_ui = True` 明示でさらに確実にする

### `run_for_agent` MCPツールの実装経路

- 決定内容:
    - 内部で `argparse.Namespace` を構築し、`run_pipeline(args, commands, config)` を直接呼び出す
    - 引数は `paths` / `commands` / `fail_fast` の3つのみMCPクライアントから受け付ける
    - 他のフラグ（no-archive・no-cache・config・output-format）は固定とする。archive・cacheは有効、設定はCWDの `pyproject.toml` を使用、`output-format=jsonl` の出力先は一時ファイルとする
    - `run_pipeline()` の戻り値を `tuple[int, str | None]` に拡張して `exit_code` と `run_id` を同時に返す。MCPツール側では `run_id is None` の場合を `RuntimeError` で弾き、実行アーカイブを強制有効化する設定と組み合わせて `run_id` 返却を契約として保証する
- 理由:
    - `run(sys_args=[...])` 経由でargparseに渡すとエラーメッセージがstderrへ書かれる制御が困難で、MCPツール側でのエラー整形ができない
    - `argparse.Namespace` 直接構築なら引数検証をMCPツール側（Pydantic）に任せられる
    - `commands` は既存 `--commands` と同じセマンティクス（カンマ区切り文字列ではなくツール名リスト）で受け取る

### MCPツール戻り値はPydantic BaseModelで構造化する

- 決定内容: Pydantic BaseModel派生の6クラスを `mcp_.py` 内で定義する。
  `RunSummaryModel` ・ `ToolSummaryModel` ・ `DiagnosticModel` ・ `RunOverviewModel` ・ `ToolDiagnosticsModel` ・ `RunForAgentResult`
- 理由:
    - FastMCPはBaseModelの `Field(description=...)` をMCPスキーマへ自動反映し、LLMエージェント側で引数・戻り値の意味を把握しやすい
    - 既存の `archive.ArchiveStore` が返す `dict[str, Any]` をBaseModelに一度通すことで、必須フィールドの型保証が得られる
    - パートB〜Eで確立したJSONLスキーマ（`run_id`・`rule_url`・`retry_command` 等）のフィールド名と揃え、CLI側の出力との認知負荷差を減らす

### 依存は本体必須のまま変えない

- 決定内容: `mcp>=1.0` は `pyproject.toml` の `[project.dependencies]` のまま。optional extrasへの分離はしない
- 理由:
    - v3の確定設計判断（作業ステータス「確定済みの設計判断」）に従う
    - MCPサーバー同梱はv3の目玉機能の1つで、「pyfltrを入れれば即座にMCPとして使える」体験を崩さない
    - `mcp` パッケージは `httpx` ・ `starlette` ・ `uvicorn` 等を引き込むが、本体依存としての受容はユーザー合意済み

## 却下した代替案

- low-level `mcp.server.Server` を直接使う案 — inputSchemaを手書きする記述量が増え、FastMCPで得られるPydantic自動スキーマ生成の利点が失われる。細かなcapability制御を必要としない本パートでは利点がない
- `pyfltr mcp --http` などで複数トランスポートを提供する案 — stdio特化のシンプルな起動形態に絞り、配布・運用・認証の設計判断を先送りしない。HTTP/SSEトランスポートが必要になった時点で別パートで検討する
- 依存をoptional extras (`pyfltr[mcp]`) へ分離する案 — v3確定設計判断（MCPサーバー同梱・`mcp`本体必須）に反する。体験の一貫性を優先する
- `run_for_agent` で `no-archive` / `no-cache` / `config` / `output-format` をMCPパラメーターとして公開する案。
    却下理由: パラメーター増加分だけMCPスキーマが肥大化し、stdio隔離も複雑化する。`pyproject.toml` と環境変数で制御できる項目はCWD依存のままとし、MCPクライアント側の制御ポイントは最小化する
- `run_for_agent` 実装を `subprocess.run(["pyfltr", "run-for-agent", ...])` で外部プロセス起動する案。
    却下理由: stdio隔離が自然になる利点はあるが、FastMCPサーバーとlintプロセスが並行稼働する際のプロセス管理・`PYFLTR_CACHE_DIR` 伝搬・`TERM` シグナルハンドリング・テストの安定性で不利。同一プロセス内で構造化出力を抑止する方が制御しやすい
- 実行系ツールを `run` / `ci` / `fast` / `run-for-agent` の4サブコマンドすべて露出する案。
    却下理由: MCPからの実行はエージェント連携が想定用途のため、`run-for-agent` 相当1本に絞る。他のサブコマンドが必要なケースは従来通りCLIで呼ぶ
- MCPツール名をCLIサブコマンドと完全一致させる案（`list-runs` / `show-run`）。
    却下理由: ハイフンはPythonの `@mcp.tool()` 名として非推奨。アンダースコア形式（`list_runs` ・ `show_run`）へ置換する。CLI側表記は変えない
- `run_pipeline()` の戻り値は変えず、MCPツール側で `ArchiveStore.list_runs(limit=1)` を呼んで最新 `run_id` を取得する案。
    却下理由: 同一ユーザーキャッシュを参照する並行プロセスが存在する場合に、別runの `run_id` を誤って拾うリスクがある
- `run_pipeline()` に `on_run_id: Callable[[str], None]` コールバック引数を追加する案。
    却下理由: 呼び出し側で採番値を受け取るための同期補助が必要になり、`_run_impl` からの呼び出しでも余計な一時変数が増える。タプル戻り値の方が素直

## 関連ファイル

実装配置:

- `pyfltr/mcp_.py`（新規）: FastMCPサーバー本体・5ツール・stdio隔離ロジック・`register_subparsers()`
- `pyfltr/main.py`: `_ALL_SUBCOMMANDS` へ `mcp` 追加・`build_parser()` で `pyfltr.mcp_.register_subparsers()` 委譲・`run()` にディスパッチ分岐追加
- `tests/mcp_test.py`（新規）: FastMCPサーバー起動・5ツールの動作確認・stdio隔離の検証

ドキュメント:

- `docs/guide/usage.md`: `pyfltr mcp` サブコマンド節を追加。MCPツール一覧と用途、起動手順、MCPクライアント設定例
- `docs/v3/index.md`: パートF主題のリンク追加
- `docs/v3/作業ステータス.md`: 進捗表と本内訳を更新
- `mkdocs.yml`: navへの `v3/MCPサーバー化.md` 追加、llmstxt `markdown_description` にMCPサーバー概要追記
- `CHANGELOG.md`: `[Unreleased] ### 追加` に `pyfltr mcp` サブコマンドとMCPツール群を追加
- `CLAUDE.md`: MCPサーバー経由のアーカイブ参照・実行経路の案内追加
- `README.md`: MCPサーバー同梱の告知とMCPクライアント連携例

## 関連ドキュメント

- [実行アーカイブ](実行アーカイブ.md): 読み取り対象となるディレクトリ構造
- [詳細参照サブコマンド](詳細参照サブコマンド.md): 読み取り系MCPツールが共有する `ArchiveStore` 読み取り経路
- [パイプライン機能拡張](パイプライン機能拡張.md): `run_for_agent` MCPツールが呼び出す `run_pipeline` の構造
- [JSONL 出力拡張](JSONL出力拡張.md): `show_run_diagnostics` が返す `DiagnosticEntry` のスキーマ
