"""MCPサーバー本体。

``pyfltr mcp`` サブコマンドで stdio トランスポートの MCP サーバーを起動する。
FastMCP を用いて 5 ツール（読み取り系 4 件・実行系 1 件）を公開し、
LLM エージェントが pyfltr の実行と実行アーカイブ参照を直接利用できるようにする。

実行系を ``run-for-agent`` 相当 1 本に絞っているのは、エージェント連携用途では
``ci`` / ``run`` / ``fast`` の差分を露出する必要が薄く、パラメーター数を抑えて
MCP スキーマを単純化するため。``no-archive`` / ``no-cache`` / ``config`` /
``output-format`` などの実行制御フラグも MCP 側へは露出させず、エージェント側の
スキーマ肥大化と stdio 隔離の複雑化を避ける。

サフィックス付きモジュール名 (``mcp_.py``) はサードパーティ ``mcp`` パッケージ
との import 衝突事故を予防するため (``warnings_.py`` と同じ方針)。
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

import pyfltr.archive
import pyfltr.config
import pyfltr.runs

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic BaseModel 群 (MCPスキーマに自動反映される)
# ---------------------------------------------------------------------------


class RunSummaryModel(pydantic.BaseModel):
    """run 一覧の 1 件分サマリ。``list_runs`` ツールの戻り値要素。"""

    run_id: str = pydantic.Field(description="run の識別子 (ULID)。")
    started_at: str | None = pydantic.Field(default=None, description="実行開始日時 (ISO 8601 形式)。")
    finished_at: str | None = pydantic.Field(default=None, description="実行完了日時 (ISO 8601 形式)。")
    exit_code: int | None = pydantic.Field(default=None, description="終了コード。0 = 成功、1 = 失敗。")
    commands: list[str] = pydantic.Field(default_factory=list, description="実行したコマンド名の一覧。")
    files: int | None = pydantic.Field(default=None, description="対象ファイル数。")


class CommandSummaryModel(pydantic.BaseModel):
    """コマンドごとのサマリ。``show_run`` ツールの戻り値内要素。"""

    command: str | None = pydantic.Field(default=None, description="コマンド名。")
    status: str | None = pydantic.Field(
        default=None,
        description="実行ステータス (succeeded / formatted / failed / skipped)。",
    )
    has_error: bool | None = pydantic.Field(default=None, description="エラーが発生したか否か。")
    diagnostics: int | None = pydantic.Field(default=None, description="diagnostic の件数。")


class DiagnosticMessageModel(pydantic.BaseModel):
    """集約 diagnostic 内の 1 指摘分。``DiagnosticModel.messages`` の要素。"""

    line: int | None = pydantic.Field(default=None, description="行番号。")
    col: int | None = pydantic.Field(default=None, description="列番号。")
    rule: str | None = pydantic.Field(default=None, description="ルール識別子。")
    severity: str | None = pydantic.Field(
        default=None,
        description="severity（error / warning / info）。未対応ツールは None。",
    )
    fix: str | None = pydantic.Field(default=None, description="自動修正可能な場合の修正内容。")
    msg: str | None = pydantic.Field(default=None, description="エラーメッセージ。")


class DiagnosticModel(pydantic.BaseModel):
    """``(command, file)`` 単位で集約された diagnostic エントリ。

    ``show_run_diagnostics`` ツールの戻り値内要素。``messages`` に個別指摘を保持する。
    """

    command: str | None = pydantic.Field(default=None, description="コマンド名。")
    file: str | None = pydantic.Field(default=None, description="対象ファイルパス。")
    messages: list[DiagnosticMessageModel] = pydantic.Field(
        default_factory=list,
        description="``(line, col, rule)`` 昇順で並ぶ個別指摘のリスト。",
    )


class RunOverviewModel(pydantic.BaseModel):
    """run の概要（meta + コマンド別サマリ）。``show_run`` ツールの戻り値。"""

    run_id: str = pydantic.Field(description="run の識別子 (ULID)。")
    meta: dict[str, typing.Any] = pydantic.Field(description="run の meta 情報（read_meta の戻り値）。")
    commands: list[CommandSummaryModel] = pydantic.Field(description="コマンド別サマリ一覧。")


class CommandDiagnosticsModel(pydantic.BaseModel):
    """コマンドの詳細情報（tool.json + diagnostics.jsonl 全件）。``show_run_diagnostics`` ツールの戻り値。

    ``hint_urls`` はPython内部では ``hint_urls`` 属性で扱うが、外部スキーマ（MCPクライアント向けの
    シリアライズ結果）では ``hint-urls`` キーで出す。serialization_alias のみを設定することで、
    入力側は従来通り属性名でコンストラクトできつつ、出力側はJSONL本体・``tool.json`` と
    キー名を揃えられる。
    """

    command_meta: dict[str, typing.Any] = pydantic.Field(description="コマンドの meta 情報（tool.json の内容）。")
    diagnostics: list[DiagnosticModel] = pydantic.Field(description="diagnostic の全件一覧。")
    hint_urls: dict[str, str] | None = pydantic.Field(
        default=None,
        serialization_alias="hint-urls",
        description="rule ID → ドキュメントURLの辞書。URLを生成できた rule のみ含める。",
    )


class RunForAgentResult(pydantic.BaseModel):
    """``run_for_agent`` ツールの戻り値。"""

    run_id: str | None = pydantic.Field(
        default=None,
        description="実行アーカイブの参照キー (ULID)。early exit 時は None。",
    )
    exit_code: int = pydantic.Field(description="終了コード。0 = 成功、1 = 失敗。")
    failed: list[str] = pydantic.Field(description="失敗したコマンド名の一覧。")
    commands: list[CommandSummaryModel] = pydantic.Field(
        default_factory=list,
        description="コマンド別サマリ一覧（status・has_error・diagnostics 件数）。",
    )
    skipped_reason: str | None = pydantic.Field(
        default=None,
        description="early exit が発生した理由。run が実行されなかった場合に設定される。",
    )
    schema_hints: dict[str, str] = pydantic.Field(
        default_factory=dict,
        description="JSONL 出力フィールドの意味を補足する英語ガイド（短縮版）。",
    )
    retry_commands: dict[str, str] = pydantic.Field(
        default_factory=dict,
        description="失敗コマンドの再実行シェルコマンド辞書（コマンド名 → shell 文字列）。成功・cached は省略。",
    )


# ---------------------------------------------------------------------------
# エラー変換ヘルパー
# ---------------------------------------------------------------------------


def _raise_mcp_error(msg: str) -> typing.Never:
    """MCPクライアントへエラーとして返すための例外を送出する。

    FastMCP は ``ValueError`` をツールエラーとして JSON-RPC エラーレスポンスに変換する。
    """
    raise ValueError(msg)


def _resolve_run_id_or_raise(store: pyfltr.archive.ArchiveStore, raw: str) -> str:
    """``resolve_run_id`` の結果を返し、エラー時は MCP エラーへ変換する。"""
    try:
        return pyfltr.runs.resolve_run_id(store, raw)
    except pyfltr.runs.RunIdError as e:
        _raise_mcp_error(str(e))


# ---------------------------------------------------------------------------
# FastMCP ツール関数群 (公開名は @mcp.tool(name=...) で明示)
# ---------------------------------------------------------------------------

# _build_server() 内で登録するため、ここではデコレーターを付けない。
# 公開名は _build_server() で @mcp.tool(name="...") によって明示的に設定する。
# 公開名はアンダースコア区切り (``list_runs`` 等) を採用する。CLI サブコマンドの
# ハイフン形式 (``list-runs``) とは異なるが、``@mcp.tool()`` のスキーマ名規則上
# ハイフンは非推奨で互換性のある FastMCP 経路もアンダースコア前提のため。


async def _tool_list_runs(limit: int = 20) -> list[RunSummaryModel]:
    """実行アーカイブに保存された run 一覧を新しい順で返す。

    対応CLI: ``pyfltr list-runs``
    """
    store = pyfltr.archive.ArchiveStore()
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
    """指定 run の meta 情報とコマンド別サマリを返す。

    ``run_id`` は ULID 完全一致・前方一致・``latest`` エイリアスを受け付ける。

    対応CLI: ``pyfltr show-run <run_id>``
    """
    store = pyfltr.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    try:
        meta = store.read_meta(resolved)
    except FileNotFoundError:
        _raise_mcp_error(f"run_id が見つからない: {resolved}")
    command_summaries = pyfltr.runs._collect_tool_summaries(store, resolved)  # noqa: SLF001  # pylint: disable=protected-access
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
    """指定 run・コマンドの tool.json と diagnostics.jsonl 全件を返す。

    ``diagnostics`` は ``(command, file)`` 単位の集約形式で、個別指摘は ``messages`` に並ぶ。
    rule→URL辞書 ``hint-urls`` は tool.json 由来でそのまま返す。
    ``commands`` に複数を指定すると、要素ごとの結果を入力順で返す。

    対応CLI: ``pyfltr show-run <run_id> --commands <name1>,<name2>``
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.archive.ArchiveStore()
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
        hint_urls = command_meta.get("hint-urls") if isinstance(command_meta.get("hint-urls"), dict) else None
        results.append(CommandDiagnosticsModel(command_meta=command_meta, diagnostics=diagnostics, hint_urls=hint_urls))
    return results


async def _tool_show_run_output(run_id: str, commands: list[str]) -> dict[str, str]:
    """指定 run・コマンドの output.log 全文を返す。

    戻り値はコマンド名→全文の辞書。``commands`` に複数を指定すると入力順で各全文を返す。

    対応CLI: ``pyfltr show-run <run_id> --commands <name> --output``（単一指定のみ）
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.archive.ArchiveStore()
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
    """指定パスに対して lint/format/test を実行し、結果を返す。

    ``run-for-agent`` サブコマンド相当（JSONL 出力既定・fix ステージ有効・
    formatter 書き換えは成功扱い）で動作する。
    実行アーカイブは常に有効化され、``run_id`` を戻り値に含む。
    early exit（直前 run なし・失敗ツールなし・対象ファイル交差が空）の場合は
    ``run_id=None`` ・ ``skipped_reason`` に理由を設定して返す。

    対応CLI: ``pyfltr run-for-agent``

    Args:
        paths: 実行対象のファイルまたはディレクトリのパス一覧。
        commands: 実行するコマンド名のリスト。省略時はプロジェクト設定の全コマンドを使用する。
        fail_fast: True の場合、1 ツールでもエラーが発生した時点で残りを打ち切る。
        only_failed: True の場合、直前 run の失敗ツール・失敗ファイルのみ再実行する。
        from_run: ``only_failed=True`` 時の参照 run_id（前方一致・``latest`` 可）。
            ``only_failed=False`` かつ ``from_run`` 指定は ValueError。
    """
    if from_run is not None and not only_failed:
        _raise_mcp_error("from_run は only_failed=True のときのみ指定できます。")

    # run-for-agent サブコマンド相当の既定値で Namespace を構築する。
    # _apply_subcommand_defaults の結果と同等になるよう各フラグを設定する。
    # ``run(sys_args=[...])`` 経由で argparse に渡す案は不採用。argparse の
    # エラーメッセージ出力先 (stderr) を MCP ツール側で整形する制御が困難で、
    # 引数検証に失敗してもクライアントへエラーを返せない。``Namespace`` を
    # 直接組み立てれば、引数検証は MCP ツール側 (Pydantic スキーマ) に
    # 任せられる。
    # 外部プロセス起動 (``subprocess.run(["pyfltr", "run-for-agent", ...])``)
    # 案も不採用。stdio 隔離は自然になるが、プロセス管理・``PYFLTR_CACHE_DIR``
    # 伝搬・``TERM`` シグナル・テスト安定性の面で同一プロセスより不利。
    commands_str: str | None = ",".join(commands) if commands else None
    args = argparse.Namespace(
        targets=[pathlib.Path(p) for p in paths],
        commands=commands_str,
        fail_fast=fail_fast,
        only_failed=only_failed,
        from_run=from_run,
        no_archive=False,  # アーカイブ必須化のため明示的に False
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

    # ``pyfltr.main`` は ``mcp_`` をトップレベルで import しているため、本モジュールからの
    # 参照は循環 import を避けるためブロック内 import とする。
    import pyfltr.main as _main  # pylint: disable=import-outside-toplevel,cyclic-import

    # 構造化出力を一時ファイルへ誘導して stdout 汚染を防ぐ。
    # NamedTemporaryFile をコンテキストマネージャーで使い、close 後もパスを残す（delete=False）。
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)

    # MCP の stdout は JSON-RPC フレームが占有するため、text_logger は run_pipeline 側で
    # stderr に強制する（force_text_on_stderr=True）。
    # 構造化出力は一時ファイル経由（FileHandler）となり stdout を汚染しない。
    args.output_file = tmp_path
    try:
        config = pyfltr.config.load_config()
        # アーカイブを強制有効化する。MCPツールは run_id を返す契約を保証する。
        config.values["archive"] = True

        commands_list: list[str] = pyfltr.config.resolve_aliases(
            (args.commands or ",".join(config.command_names)).split(","),
            config,
        )

        exit_code, run_id = _main.run_pipeline(args, commands_list, config, force_text_on_stderr=True)
    finally:
        # 一時ファイルを削除する（存在しない場合はそのまま無視する）
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)

    import pyfltr.llm_output as _llm_output  # pylint: disable=import-outside-toplevel,cyclic-import

    schema_hints = _llm_output.get_schema_hints(full=False)

    # only_failed による early exit: run_id が None のとき実行がスキップされた。
    if run_id is None:
        return RunForAgentResult(
            run_id=None,
            exit_code=exit_code,
            failed=[],
            commands=[],
            skipped_reason=(
                "only_failed が有効ですが実行対象がありませんでした（直前 run なし・失敗ツールなし・対象ファイル交差なし）。"
            ),
            schema_hints=schema_hints,
            retry_commands={},
        )

    # コマンド別サマリを最新アーカイブから集計する。
    store = pyfltr.archive.ArchiveStore()
    try:
        command_summaries = pyfltr.runs._collect_tool_summaries(store, run_id)  # noqa: SLF001  # pylint: disable=protected-access
    except Exception:  # pylint: disable=broad-exception-caught
        command_summaries = []

    commands_model = [CommandSummaryModel.model_validate(entry) for entry in command_summaries]
    failed_commands = [c.command for c in commands_model if c.has_error and c.command]

    # 失敗コマンドの retry_command をアーカイブから収集する（F7）。
    retry_commands: dict[str, str] = {}
    for summary_entry in command_summaries:
        cmd_name = summary_entry.get("command")
        if summary_entry.get("has_error") and cmd_name:
            try:
                tool_meta = store.read_tool_meta(run_id, cmd_name)
                rc = tool_meta.get("retry_command")
                if rc:
                    retry_commands[cmd_name] = rc
            except Exception:  # pylint: disable=broad-exception-caught  # tool.json 読み取り失敗は非致命的
                logger.debug("retry_command 取得失敗: command=%s", cmd_name, exc_info=True)

    return RunForAgentResult(
        run_id=run_id,
        exit_code=exit_code,
        failed=failed_commands,
        commands=commands_model,
        schema_hints=schema_hints,
        retry_commands=retry_commands,
    )


# ---------------------------------------------------------------------------
# FastMCP サーバー組み立て
# ---------------------------------------------------------------------------


def _build_server() -> typing.Any:
    """FastMCP サーバーインスタンスを生成し、5 ツールを登録して返す。

    公開名は ``@mcp.tool(name=...)`` で明示し、Python 側の関数名（``_tool_*``）
    とは独立したスキーマ名（``list_runs`` 等）を維持する。
    戻り値型を ``typing.Any`` とするのは、``mcp`` 未インストール環境でも本モジュールの
    import が壊れないよう ``FastMCP`` を本関数内で局所 import する設計に合わせるため。
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
            " 戻り値に schema_hints（JSONL フィールド解説）と retry_commands（失敗コマンドの再実行シェルコマンド）を含む。"
        ),
    )(_tool_run_for_agent)

    return mcp


# ---------------------------------------------------------------------------
# サブコマンド登録・エントリポイント
# ---------------------------------------------------------------------------


def register_subparsers(subparsers: typing.Any) -> None:
    """``mcp`` サブパーサーを登録する。

    ``subparsers`` は ``ArgumentParser.add_subparsers()`` の戻り値
    (``argparse._SubParsersAction``) を想定する。
    """
    subparsers.add_parser(
        "mcp",
        help="MCP サーバーを stdio で起動する。",
    )


def execute_mcp(args: argparse.Namespace) -> int:
    """``mcp`` サブコマンドの処理本体。

    stdio トランスポートで MCP サーバーを起動する。
    起動直後に root logger をstderrへ向けて JSON-RPC フレームの stdout 汚染を防ぐ。
    FastMCP の ``run(transport="stdio")`` は stdin EOF で終了する。
    """
    del args  # noqa: F841

    # stdio トランスポートでは stdout を JSON-RPC フレームが専有するため、
    # ロギングは必ず stderr へ向ける。
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        server = _build_server()
        server.run(transport="stdio")
        return 0
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MCP サーバーの起動に失敗した: %s", e)
        return 1
