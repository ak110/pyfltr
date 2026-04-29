"""MCPサーバー本体。

`pyfltr mcp`サブコマンドでstdioトランスポートのMCPサーバーを起動する。
FastMCPを用いて5ツール（読み取り系4件・実行系1件）を公開し、
LLMエージェントがpyfltrの実行と実行アーカイブ参照を直接利用できるようにする。

実行系を`run-for-agent`相当1本に絞っているのは、エージェント連携用途では
`ci`/`run`/`fast`の差分を露出する必要が薄く、パラメーター数を抑えて
MCPスキーマを単純化するため。`no-archive`/`no-cache`/`config`/
`output-format`などの実行制御フラグもMCP側へは露出させず、エージェント側の
スキーマ肥大化とstdio隔離の複雑化を避ける。

サフィックス付きモジュール名（`mcp_.py`）はサードパーティ`mcp`パッケージ
とのimport衝突事故を予防するため（`warnings_.py`と同じ方針）。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import pathlib
import sys
import tempfile
import typing

import pydantic

import pyfltr.config.config
import pyfltr.state.archive
import pyfltr.state.runs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic BaseModel群（MCPスキーマに自動反映される）
# ---------------------------------------------------------------------------


class RunSummaryModel(pydantic.BaseModel):
    """run一覧の1件分サマリ。`list_runs`ツールの戻り値要素。"""

    run_id: str = pydantic.Field(description="runの識別子（ULID）。")
    started_at: str | None = pydantic.Field(default=None, description="実行開始日時（ISO 8601形式）。")
    finished_at: str | None = pydantic.Field(default=None, description="実行完了日時（ISO 8601形式）。")
    exit_code: int | None = pydantic.Field(default=None, description="終了コード。0 = 成功、1 = 失敗。")
    commands: list[str] = pydantic.Field(default_factory=list, description="実行したコマンド名の一覧。")
    files: int | None = pydantic.Field(default=None, description="対象ファイル数。")


class CommandSummaryModel(pydantic.BaseModel):
    """コマンドごとのサマリ。`show_run`ツールの戻り値内要素。"""

    command: str | None = pydantic.Field(default=None, description="コマンド名。")
    status: str | None = pydantic.Field(
        default=None,
        description="実行ステータス（succeeded / formatted / failed / skipped）。",
    )
    has_error: bool | None = pydantic.Field(default=None, description="エラーが発生したか否か。")
    diagnostics: int | None = pydantic.Field(default=None, description="diagnosticの件数。")


class DiagnosticMessageModel(pydantic.BaseModel):
    """集約diagnostic内の1指摘分。`DiagnosticModel.messages`の要素。"""

    line: int | None = pydantic.Field(default=None, description="行番号。")
    col: int | None = pydantic.Field(default=None, description="列番号。")
    end_line: int | None = pydantic.Field(
        default=None,
        description="違反範囲の終端行。範囲を返すツール（現状textlintのみ）で設定される。",
    )
    end_col: int | None = pydantic.Field(
        default=None,
        description=(
            "違反範囲の終端列。範囲を返すツール（現状textlintのみ）で設定される。"
            "textlintはノード先頭からの累積位置を返す仕様で、行内オフセットではない。"
        ),
    )
    rule: str | None = pydantic.Field(default=None, description="ルール識別子。")
    severity: str | None = pydantic.Field(
        default=None,
        description="severity（error / warning / info）。未対応ツールはNone。",
    )
    fix: str | None = pydantic.Field(default=None, description="自動修正可能な場合の修正内容。")
    msg: str | None = pydantic.Field(default=None, description="エラーメッセージ。")


class DiagnosticModel(pydantic.BaseModel):
    """`(command, file)`単位で集約されたdiagnosticエントリ。

    `show_run_diagnostics`ツールの戻り値内要素。`messages`に個別指摘を保持する。
    """

    command: str | None = pydantic.Field(default=None, description="コマンド名。")
    file: str | None = pydantic.Field(default=None, description="対象ファイルパス。")
    messages: list[DiagnosticMessageModel] = pydantic.Field(
        default_factory=list,
        description="`(line, col, rule)`昇順で並ぶ個別指摘のリスト。",
    )


class RunOverviewModel(pydantic.BaseModel):
    """runの概要（meta + コマンド別サマリ）。`show_run`ツールの戻り値。"""

    run_id: str = pydantic.Field(description="runの識別子（ULID）。")
    meta: dict[str, typing.Any] = pydantic.Field(description="runのmeta情報（`read_meta`の戻り値）。")
    commands: list[CommandSummaryModel] = pydantic.Field(description="コマンド別サマリ一覧。")


class CommandDiagnosticsModel(pydantic.BaseModel):
    """コマンドの詳細情報（tool.json + diagnostics.jsonl全件）。`show_run_diagnostics`ツールの戻り値。

    JSONL本体・`tool.json`の双方で`hint_urls`キー（アンダースコア区切り）を採用するため、
    Pydantic側でも属性名・出力キー名ともに`hint_urls`で揃える。
    同様に`hints`キーも`tool.json`と同名で揃える。
    """

    command_meta: dict[str, typing.Any] = pydantic.Field(description="コマンドのmeta情報（`tool.json`の内容）。")
    diagnostics: list[DiagnosticModel] = pydantic.Field(description="diagnosticの全件一覧。")
    hint_urls: dict[str, str] | None = pydantic.Field(
        default=None,
        description="rule ID → ドキュメントURLの辞書。URLを生成できたruleのみ含める。",
    )
    hints: dict[str, str] | None = pydantic.Field(
        default=None,
        description="rule ID → 短い修正ヒント文字列の辞書。ヒントを持つruleのみ含める。",
    )


class RunForAgentResult(pydantic.BaseModel):
    """`run_for_agent`ツールの戻り値。"""

    run_id: str | None = pydantic.Field(
        default=None,
        description="実行アーカイブの参照キー（ULID）。early exit時はNone。",
    )
    exit_code: int = pydantic.Field(description="終了コード。0 = 成功、1 = 失敗。")
    failed: list[str] = pydantic.Field(description="失敗したコマンド名の一覧。")
    commands: list[CommandSummaryModel] = pydantic.Field(
        default_factory=list,
        description="コマンド別サマリ一覧（status・has_error・diagnostics件数）。",
    )
    skipped_reason: str | None = pydantic.Field(
        default=None,
        description="early exitが発生した理由。runが実行されなかった場合に設定される。",
    )
    retry_commands: dict[str, str] = pydantic.Field(
        default_factory=dict,
        description="失敗コマンドの再実行シェルコマンド辞書（コマンド名 → shell文字列）。成功・cachedは省略。",
    )


# ---------------------------------------------------------------------------
# エラー変換ヘルパー
# ---------------------------------------------------------------------------


def _raise_mcp_error(msg: str) -> typing.Never:
    """MCPクライアントへエラーとして返すための例外を送出する。

    FastMCPは`ValueError`をツールエラーとしてJSON-RPCエラーレスポンスに変換する。
    """
    raise ValueError(msg)


def _resolve_run_id_or_raise(store: pyfltr.state.archive.ArchiveStore, raw: str) -> str:
    """`resolve_run_id`の結果を返し、エラー時はMCPエラーへ変換する。"""
    try:
        return pyfltr.state.runs.resolve_run_id(store, raw)
    except pyfltr.state.runs.RunIdError as e:
        _raise_mcp_error(str(e))


# ---------------------------------------------------------------------------
# FastMCPツール関数群（公開名は@mcp.tool(name=...)で明示）
# ---------------------------------------------------------------------------

# _build_server()内で登録するため、ここではデコレーターを付けない。
# 公開名は_build_server()で@mcp.tool(name="...")によって明示的に設定する。
# 公開名はアンダースコア区切り（`list_runs`等）を採用する。CLIサブコマンドの
# ハイフン形式（`list-runs`）とは異なるが、`@mcp.tool()`のスキーマ名規則上
# ハイフンは非推奨で互換性のあるFastMCP経路もアンダースコア前提のため。


async def _tool_list_runs(limit: int = 20) -> list[RunSummaryModel]:
    """実行アーカイブに保存されたrun一覧を新しい順で返す。

    対応CLI: `pyfltr list-runs`
    """
    store = pyfltr.state.archive.ArchiveStore()
    summaries = store.list_runs(limit=limit)
    return [
        RunSummaryModel(
            run_id=s.run_id,
            started_at=s.started_at,
            finished_at=s.finished_at,
            exit_code=s.exit_code,
            commands=list(s.commands),
            files=s.files,
        )
        for s in summaries
    ]


async def _tool_show_run(run_id: str) -> RunOverviewModel:
    """指定runのmeta情報とコマンド別サマリを返す。

    `run_id`はULID完全一致・前方一致・`latest`エイリアスを受け付ける。

    対応CLI: `pyfltr show-run <run_id>`
    """
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    try:
        meta = store.read_meta(resolved)
    except FileNotFoundError:
        _raise_mcp_error(f"run_id が見つからない: {resolved}")
    command_summaries = pyfltr.state.runs._collect_tool_summaries(store, resolved)  # noqa: SLF001  # pylint: disable=protected-access
    commands = [
        CommandSummaryModel(
            command=entry.get("command"),
            status=entry.get("status"),
            has_error=entry.get("has_error"),
            diagnostics=entry.get("diagnostics"),
        )
        for entry in command_summaries
    ]
    return RunOverviewModel(run_id=resolved, meta=meta, commands=commands)


async def _tool_show_run_diagnostics(run_id: str, commands: list[str]) -> list[CommandDiagnosticsModel]:
    """指定run・コマンドのtool.jsonとdiagnostics.jsonl全件を返す。

    `diagnostics`は`(command, file)`単位の集約形式で、個別指摘は`messages`に並ぶ。
    rule→URL辞書`hint_urls`はtool.json由来でそのまま返す。
    `commands`に複数を指定すると、要素ごとの結果を入力順で返す。

    対応CLI: `pyfltr show-run <run_id> --commands <name1>,<name2>`
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    results: list[CommandDiagnosticsModel] = []
    for command in commands:
        try:
            command_meta = store.read_tool_meta(resolved, command)
            diagnostics_raw = store.read_tool_diagnostics(resolved, command)
        except FileNotFoundError:
            _raise_mcp_error(f"run {resolved} にコマンド {command!r} の結果が保存されていない。")
        diagnostics = [
            DiagnosticModel(
                command=d.get("command", d.get("tool")),
                file=d.get("file"),
                messages=[DiagnosticMessageModel(**m) for m in d.get("messages", [])],
            )
            for d in diagnostics_raw
        ]
        hint_urls = command_meta.get("hint_urls") if isinstance(command_meta.get("hint_urls"), dict) else None
        hints = command_meta.get("hints") if isinstance(command_meta.get("hints"), dict) else None
        results.append(
            CommandDiagnosticsModel(command_meta=command_meta, diagnostics=diagnostics, hint_urls=hint_urls, hints=hints)
        )
    return results


async def _tool_show_run_output(run_id: str, commands: list[str]) -> dict[str, str]:
    """指定run・コマンドのoutput.log全文を返す。

    戻り値はコマンド名→全文の辞書。`commands`に複数を指定すると入力順で各全文を返す。

    対応CLI: `pyfltr show-run <run_id> --commands <name> --output`（単一指定のみ）
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    outputs: dict[str, str] = {}
    for command in commands:
        try:
            outputs[command] = store.read_tool_output(resolved, command)
        except FileNotFoundError:
            _raise_mcp_error(f"run {resolved} にコマンド {command!r} の結果が保存されていない。")
    return outputs


async def _tool_run_for_agent(
    paths: list[str],
    commands: list[str] | None = None,
    fail_fast: bool = False,
    only_failed: bool = False,
    from_run: str | None = None,
) -> RunForAgentResult:
    """指定パスに対してlint/format/testを実行し、結果を返す。

    `run-for-agent`サブコマンド相当（JSONL出力既定・fixステージ有効・
    formatter書き換えは成功扱い）で動作する。
    実行アーカイブは常に有効化され、`run_id`を戻り値に含む。
    early exit（直前runなし・失敗ツールなし・対象ファイル交差が空）の場合は
    `run_id=None`・`skipped_reason`に理由を設定して返す。

    対応CLI: `pyfltr run-for-agent`

    Args:
        paths: 実行対象のファイルまたはディレクトリのパス一覧。
        commands: 実行するコマンド名のリスト。省略時はプロジェクト設定の全コマンドを使用する。
        fail_fast: Trueの場合、1ツールでもエラーが発生した時点で残りを打ち切る。
        only_failed: Trueの場合、直前runの失敗ツール・失敗ファイルのみ再実行する。
        from_run: `only_failed=True`時の参照run_id（前方一致・`latest`可）。
            `only_failed=False`かつ`from_run`指定はValueError。
    """
    if from_run is not None and not only_failed:
        _raise_mcp_error("from_run は only_failed=True のときのみ指定できます。")

    # run-for-agentサブコマンド相当の既定値でNamespaceを構築する。
    # _apply_subcommand_defaultsの結果と同等になるよう各フラグを設定する。
    # `run(sys_args=[...])`経由でargparseに渡す案は不採用。argparseの
    # エラーメッセージ出力先（stderr）をMCPツール側で整形する制御が困難で、
    # 引数検証に失敗してもクライアントへエラーを返せない。`Namespace`を
    # 直接組み立てれば、引数検証はMCPツール側（Pydanticスキーマ）に
    # 任せられる。
    # 外部プロセス起動（`subprocess.run(["pyfltr", "run-for-agent", ...])`）
    # 案も不採用。stdio隔離は自然になるが、プロセス管理・`PYFLTR_CACHE_DIR`
    # 伝搬・`TERM`シグナル・テスト安定性の面で同一プロセスより不利。
    commands_str: str | None = ",".join(commands) if commands else None
    args = argparse.Namespace(
        targets=[pathlib.Path(p) for p in paths],
        commands=commands_str,
        fail_fast=fail_fast,
        only_failed=only_failed,
        from_run=from_run,
        no_archive=False,  # アーカイブ必須化のため明示的にFalse
        no_cache=False,
        verbose=False,
        output_format="jsonl",
        output_file=None,  # 後で一時ファイルで上書きする
        ui=None,
        no_ui=True,
        no_clear=True,
        stream=False,
        shuffle=False,
        keep_ui=False,
        ci=False,
        human_readable=False,
        no_exclude=False,
        no_gitignore=False,
        jobs=None,
        work_dir=None,
        exit_zero_even_if_formatted=True,
        include_fix_stage=True,
        no_fix=False,
        version=False,
        subcommand="run-for-agent",
    )

    # `pyfltr.cli.pipeline`は`mcp_server`をトップレベルでimportしないため、
    # 循環importの懸念は小さいが、遅延importで明示する。
    import pyfltr.cli.pipeline as _main  # pylint: disable=import-outside-toplevel

    # 構造化出力を一時ファイルへ誘導してstdout汚染を防ぐ。
    # NamedTemporaryFileをコンテキストマネージャーで使い、close後もパスを残す（delete=False）。
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)

    # MCPのstdoutはJSON-RPCフレームが占有するため、text_loggerはrun_pipeline側で
    # stderrに強制する（force_text_on_stderr=True）。
    # 構造化出力は一時ファイル経由（FileHandler）となりstdoutを汚染しない。
    args.output_file = tmp_path
    try:
        config = pyfltr.config.config.load_config()
        # アーカイブを強制有効化する。MCPツールはrun_idを返す契約を保証する。
        config.values["archive"] = True

        commands_list: list[str] = pyfltr.config.config.resolve_aliases(
            (args.commands or ",".join(config.command_names)).split(","),
            config,
        )

        exit_code, run_id = _main.run_pipeline(args, commands_list, config, force_text_on_stderr=True)
    finally:
        # 一時ファイルを削除する（存在しない場合はそのまま無視する）
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)

    # only_failedによるearly exit: run_idがNoneのとき実行がスキップされた。
    if run_id is None:
        return RunForAgentResult(
            run_id=None,
            exit_code=exit_code,
            failed=[],
            commands=[],
            skipped_reason=(
                "only_failed が有効ですが実行対象がありませんでした（直前 run なし・失敗ツールなし・対象ファイル交差なし）。"
            ),
            retry_commands={},
        )

    # コマンド別サマリを最新アーカイブから集計する。
    store = pyfltr.state.archive.ArchiveStore()
    try:
        command_summaries = pyfltr.state.runs._collect_tool_summaries(store, run_id)  # noqa: SLF001  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-exception-caught
        command_summaries = []

    commands_model = [CommandSummaryModel.model_validate(entry) for entry in command_summaries]
    failed_commands = [c.command for c in commands_model if c.has_error and c.command]

    # 失敗コマンドのretry_commandをアーカイブから収集する（F7）。
    retry_commands: dict[str, str] = {}
    for summary_entry in command_summaries:
        cmd_name = summary_entry.get("command")
        if summary_entry.get("has_error") and cmd_name:
            try:
                tool_meta = store.read_tool_meta(run_id, cmd_name)
                rc = tool_meta.get("retry_command")
                if rc:
                    retry_commands[cmd_name] = rc
            except Exception:  # pylint: disable=broad-exception-caught  # tool.json読み取り失敗は非致命的
                logger.debug("retry_command取得失敗: command=%s", cmd_name, exc_info=True)

    return RunForAgentResult(
        run_id=run_id,
        exit_code=exit_code,
        failed=failed_commands,
        commands=commands_model,
        retry_commands=retry_commands,
    )


# ---------------------------------------------------------------------------
# FastMCPサーバー組み立て
# ---------------------------------------------------------------------------


def _build_server() -> typing.Any:
    """FastMCPサーバーインスタンスを生成し、5ツールを登録して返す。

    公開名は`@mcp.tool(name=...)`で明示し、Python側の関数名（`_tool_*`）
    とは独立したスキーマ名（`list_runs`等）を維持する。
    戻り値型を`typing.Any`とするのは、`mcp`未インストール環境でも本モジュールの
    importが壊れないよう`FastMCP`を本関数内で局所importする設計に合わせるため。
    """
    try:
        from mcp.server.fastmcp import FastMCP  # pylint: disable=import-outside-toplevel
    except ImportError as e:
        raise RuntimeError("mcp ライブラリが見つからない。`pip install mcp` で導入してください。") from e

    mcp = FastMCP("pyfltr")

    mcp.tool(name="list_runs", description="実行アーカイブに保存された run 一覧を新しい順で返す。")(_tool_list_runs)
    mcp.tool(
        name="show_run", description="指定 run の meta 情報とコマンド別サマリを返す。run_id は前方一致・latest エイリアス可。"
    )(_tool_show_run)
    mcp.tool(name="show_run_diagnostics", description="指定 run・コマンドの tool.json と diagnostics 全件を返す。")(
        _tool_show_run_diagnostics
    )
    mcp.tool(name="show_run_output", description="指定 run・コマンドの output.log 全文を返す。")(_tool_show_run_output)
    mcp.tool(
        name="run_for_agent",
        description=(
            "指定パスに対して lint/format/test を実行し、run_id・終了コード・失敗コマンド名を返す。"
            " only_failed=True で直前 run の失敗ツール・失敗ファイルのみ再実行する（from_run で参照 run を指定可）。"
            " 戻り値に retry_commands（失敗コマンドの再実行シェルコマンド）を含む。"
        ),
    )(_tool_run_for_agent)

    return mcp


# ---------------------------------------------------------------------------
# サブコマンド登録・エントリポイント
# ---------------------------------------------------------------------------


def register_subparsers(subparsers: typing.Any) -> None:
    """`mcp`サブパーサーを登録する。

    `subparsers`は`ArgumentParser.add_subparsers()`の戻り値
    （`argparse._SubParsersAction`）を想定する。
    """
    subparsers.add_parser(
        "mcp",
        help="MCP サーバーを stdio で起動する。",
    )


def execute_mcp(args: argparse.Namespace) -> int:
    """`mcp`サブコマンドの処理本体。

    stdioトランスポートでMCPサーバーを起動する。
    起動直後にroot loggerをstderrへ向けてJSON-RPCフレームのstdout汚染を防ぐ。
    FastMCPの`run(transport="stdio")`はstdin EOFで終了する。
    """
    del args  # noqa: F841

    # stdioトランスポートではstdoutをJSON-RPCフレームが専有するため、
    # ロギングは必ずstderrへ向ける。
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        server = _build_server()
        server.run(transport="stdio")
        return 0
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MCP サーバーの起動に失敗した: %s", e)
        return 1
