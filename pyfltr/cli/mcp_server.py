"""MCPサーバー本体。

`pyfltr mcp`サブコマンドでstdioトランスポートのMCPサーバーを起動する。
FastMCPを用いて8ツール（読み取り系4件・実行系1件・grep/replace系3件）を公開し、
LLMエージェントがpyfltrの実行と実行アーカイブ参照を直接利用できるようにする。

実行系を`run-for-agent`相当1本に限定しているのは、エージェント連携用途では
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

import pyfltr.cli.pipeline
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.grep_.history
import pyfltr.grep_.matcher
import pyfltr.grep_.replacer
import pyfltr.grep_.scanner
import pyfltr.state.archive
import pyfltr.state.runs
from pyfltr.grep_.types import MatchRecord, ReplaceCommandMeta

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


class GrepMatchModel(pydantic.BaseModel):
    """`grep`ツールの1マッチ分。"""

    file: str = pydantic.Field(description="マッチを検出したファイルパス。")
    line: int = pydantic.Field(description="マッチ開始行番号（1-origin）。")
    col: int = pydantic.Field(description="マッチ開始列番号（1-origin、文字単位）。")
    end_col: int | None = pydantic.Field(default=None, description="マッチ終了列番号（1-origin）。")
    match_text: str = pydantic.Field(description="マッチした文字列。")
    line_text: str = pydantic.Field(description="マッチを含む行の本文（改行除く）。")
    before: list[str] = pydantic.Field(default_factory=list, description="`-B`コンテキストの前行群。")
    after: list[str] = pydantic.Field(default_factory=list, description="`-A`コンテキストの後行群。")


class GrepResultModel(pydantic.BaseModel):
    """`grep`ツールの戻り値。"""

    matches: list[GrepMatchModel] = pydantic.Field(description="マッチ一覧。")
    total_matches: int = pydantic.Field(description="全マッチ件数。")
    files_scanned: int = pydantic.Field(description="走査したファイル数。")
    exit_code: int = pydantic.Field(description="終了コード。マッチあり=0、マッチなし=1。")


class ReplaceFileChangeModel(pydantic.BaseModel):
    """`replace`ツールの1ファイル変更分。"""

    file: str = pydantic.Field(description="変更対象ファイルパス。")
    count: int = pydantic.Field(description="置換箇所数。")
    before_hash: str | None = pydantic.Field(default=None, description="変更前内容のSHA-256ハッシュ。")
    after_hash: str | None = pydantic.Field(default=None, description="変更後内容のSHA-256ハッシュ。")


class ReplaceChangeRecordModel(pydantic.BaseModel):
    """`replace`ツールの1置換箇所。`show_changes=True`時に`ReplaceResultModel.changes`へ含まれる。"""

    file: str = pydantic.Field(description="対象ファイルパス。")
    line: int = pydantic.Field(description="置換対象行番号（1-origin）。")
    col: int = pydantic.Field(description="置換開始列番号（1-origin）。")
    before_line: str = pydantic.Field(description="置換前の行本文。")
    after_line: str = pydantic.Field(description="置換後の行本文。")


class ReplaceResultModel(pydantic.BaseModel):
    """`replace`ツールの戻り値。"""

    replace_id: str | None = pydantic.Field(
        default=None,
        description="replace履歴の識別子（ULID）。dry_run=True時はNone。",
    )
    dry_run: bool = pydantic.Field(description="dry-runモードか否か。")
    files_changed: int = pydantic.Field(description="変更が発生したファイル数。")
    total_replacements: int = pydantic.Field(description="置換箇所の総数。")
    file_changes: list[ReplaceFileChangeModel] = pydantic.Field(description="ファイルごとの変更サマリ。")
    changes: list[ReplaceChangeRecordModel] = pydantic.Field(
        default_factory=list,
        description="`show_changes=True`時の各置換箇所の変更前後（空リストで省略）。",
    )
    exit_code: int = pydantic.Field(description="終了コード。0 = 成功。")


class ReplaceUndoModel(pydantic.BaseModel):
    """`replace_undo`ツールの戻り値。"""

    replace_id: str = pydantic.Field(description="undo対象のreplace識別子（ULID）。")
    restored: list[str] = pydantic.Field(description="復元に成功したファイルパスの一覧。")
    skipped: list[str] = pydantic.Field(
        description="ハッシュ不一致でスキップされたファイルパスの一覧（force=False時）。",
    )
    exit_code: int = pydantic.Field(description="終了コード。skippedあり=1、全件復元=0。")


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
    command_summaries = pyfltr.state.runs.collect_tool_summaries(store, resolved)
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

        exit_code, run_id = pyfltr.cli.pipeline.run_pipeline(args, commands_list, config, force_text_on_stderr=True)
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
        command_summaries = pyfltr.state.runs.collect_tool_summaries(store, run_id)
    except Exception:  # MCPツール戻り値の組み立て継続を優先するため全例外を吸収する
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
            except Exception:  # tool.json読み取り失敗は非致命的
                logger.debug("retry_command取得失敗: command=%s", cmd_name, exc_info=True)

    return RunForAgentResult(
        run_id=run_id,
        exit_code=exit_code,
        failed=failed_commands,
        commands=commands_model,
        retry_commands=retry_commands,
    )


async def _tool_grep(
    pattern: str,
    paths: list[str],
    ignore_case: bool = False,
    smart_case: bool = False,
    fixed_strings: bool = False,
    word_regexp: bool = False,
    line_regexp: bool = False,
    multiline: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    max_count: int = 0,
    max_total: int = 1000,
    types: list[str] | None = None,
    globs: list[str] | None = None,
    encoding: str = "utf-8",
    max_filesize: int | None = None,
    hidden: bool = False,
    no_exclude: bool = False,
    no_gitignore: bool = False,
) -> GrepResultModel:
    """指定ファイル群から正規表現パターンを検索し、マッチ一覧を返す。

    pyfltrの`exclude`/`extend-exclude`/`respect-gitignore`設定を尊重する。
    `max_total`の既定値は1000でCLI既定（無制限）より安全側に設定する。

    Args:
        pattern: 検索パターン（正規表現、または`fixed_strings=True`で固定文字列）。
        paths: 検索対象のファイルまたはディレクトリパスの一覧。
        ignore_case: 大文字小文字を区別しない。
        smart_case: パターンに大文字を含まない場合のみignore_caseを有効化する。
        fixed_strings: パターンを固定文字列として扱う。
        word_regexp: 単語境界で囲まれたマッチのみ採用する。
        line_regexp: 行全体に一致したマッチのみ採用する。
        multiline: マルチラインマッチを有効化する。
        before_context: マッチ行の前に含める行数。
        after_context: マッチ行の後に含める行数。
        max_count: ファイル単位の最大マッチ件数（0で無制限）。
        max_total: 全体の最大マッチ件数（既定1000）。
        types: 対象言語タイプの一覧（例: ["python", "ts"]）。
        globs: globパターンでの対象限定一覧。
        encoding: ファイル読み込み時のエンコーディング（既定: utf-8）。
        max_filesize: 走査対象ファイルサイズの上限（バイト単位）。
        hidden: ドットファイルも対象に含める。
        no_exclude: exclude/extend-excludeによる除外を無効化する。
        no_gitignore: .gitignoreによる除外を無効化する。
    """
    try:
        compiled = pyfltr.grep_.matcher.compile_pattern(
            [pattern],
            fixed_strings=fixed_strings,
            ignore_case=ignore_case,
            smart_case=smart_case,
            word_regexp=word_regexp,
            line_regexp=line_regexp,
            multiline=multiline,
        )
    except ValueError as exc:
        _raise_mcp_error(str(exc))

    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        _raise_mcp_error(f"設定エラー: {exc}")

    if no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if no_gitignore:
        config.values["respect-gitignore"] = False

    expanded = pyfltr.command.targets.expand_all_files(
        [pathlib.Path(p) for p in paths],
        config,
    )
    expanded = pyfltr.grep_.scanner.filter_files_by_type(expanded, types or [])
    expanded = pyfltr.grep_.scanner.filter_by_globs(expanded, globs or [])
    if not hidden:
        expanded = [p for p in expanded if not _has_hidden_segment(p)]

    files_scanned = len(expanded)
    matches: list[GrepMatchModel] = []
    for record in pyfltr.grep_.scanner.scan_files(
        expanded,
        compiled,
        before_context=before_context,
        after_context=after_context,
        max_per_file=max_count,
        max_total=max_total,
        encoding=encoding,
        max_filesize=max_filesize,
        multiline=multiline,
    ):
        if isinstance(record, MatchRecord):
            matches.append(
                GrepMatchModel(
                    file=str(record.file),
                    line=record.line,
                    col=record.col,
                    end_col=record.end_col,
                    match_text=record.match_text,
                    line_text=record.line_text,
                    before=list(record.before_lines),
                    after=list(record.after_lines),
                )
            )

    total_matches = len(matches)
    return GrepResultModel(
        matches=matches,
        total_matches=total_matches,
        files_scanned=files_scanned,
        exit_code=0 if total_matches > 0 else 1,
    )


async def _tool_replace(
    pattern: str,
    replacement: str,
    paths: list[str],
    dry_run: bool = True,
    ignore_case: bool = False,
    smart_case: bool = False,
    fixed_strings: bool = False,
    word_regexp: bool = False,
    line_regexp: bool = False,
    multiline: bool = False,
    types: list[str] | None = None,
    globs: list[str] | None = None,
    encoding: str = "utf-8",
    max_filesize: int | None = None,
    hidden: bool = False,
    exclude_files: list[str] | None = None,
    no_exclude: bool = False,
    no_gitignore: bool = False,
    show_changes: bool = False,
) -> ReplaceResultModel:
    r"""指定ファイル群へ正規表現置換を適用し、変更内容を返す。

    `dry_run=True`（既定）はファイルを変更せず変更内容のみを返す。
    `dry_run=False`を明示した場合のみ実書き込みし、`replace_id`を返す。
    `dry_run`の既定値がCLI（`False`）と異なるのはLLM暴発防止のため
    （`.claude/rules/grep-replace.md`参照）。

    Args:
        pattern: 検索パターン（正規表現）。
        replacement: 置換式（`re.sub`互換、`\\1`/`\\g<name>`参照可）。
        paths: 対象のファイルまたはディレクトリパスの一覧。
        dry_run: Trueの場合（既定）、ファイルを変更せず変更内容のみ計算する。
        ignore_case: 大文字小文字を区別しない。
        smart_case: パターンに大文字を含まない場合のみignore_caseを有効化する。
        fixed_strings: パターンを固定文字列として扱う。
        word_regexp: 単語境界で囲まれたマッチのみ採用する。
        line_regexp: 行全体に一致したマッチのみ採用する。
        multiline: マルチラインマッチを有効化する。
        types: 対象言語タイプの一覧。
        globs: globパターンでの対象限定一覧。
        encoding: ファイル読み込み・書き込み時のエンコーディング（既定: utf-8）。
        max_filesize: 走査対象ファイルサイズの上限（バイト単位）。
        hidden: ドットファイルも対象に含める。
        exclude_files: 置換対象から除外するファイルパスの一覧。
        no_exclude: exclude/extend-excludeによる除外を無効化する。
        no_gitignore: .gitignoreによる除外を無効化する。
        show_changes: Trueの場合、`changes`フィールドに各置換箇所の変更前後を含める。
    """
    if not paths:
        _raise_mcp_error("paths を 1 件以上指定してください。")

    try:
        compiled = pyfltr.grep_.matcher.compile_pattern(
            [pattern],
            fixed_strings=fixed_strings,
            ignore_case=ignore_case,
            smart_case=smart_case,
            word_regexp=word_regexp,
            line_regexp=line_regexp,
            multiline=multiline,
        )
    except ValueError as exc:
        _raise_mcp_error(str(exc))

    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        _raise_mcp_error(f"設定エラー: {exc}")

    if no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if no_gitignore:
        config.values["respect-gitignore"] = False

    expanded = pyfltr.command.targets.expand_all_files(
        [pathlib.Path(p) for p in paths],
        config,
    )
    expanded = pyfltr.grep_.scanner.filter_files_by_type(expanded, types or [])
    expanded = pyfltr.grep_.scanner.filter_by_globs(expanded, globs or [])
    if not hidden:
        expanded = [p for p in expanded if not _has_hidden_segment(p)]

    # exclude_filesによる対象限定
    if exclude_files:
        excluded = {pathlib.Path(p).resolve() for p in exclude_files}
        expanded = [p for p in expanded if p.resolve() not in excluded]

    replace_id = pyfltr.grep_.history.generate_replace_id() if not dry_run else None
    history_entries: list[dict[str, typing.Any]] = []
    file_changes: list[ReplaceFileChangeModel] = []
    change_records: list[ReplaceChangeRecordModel] = []
    total_replacements = 0
    files_changed = 0

    for file in expanded:
        if max_filesize is not None and max_filesize > 0:
            try:
                if file.stat().st_size > max_filesize:
                    continue
            except OSError:
                continue
        try:
            before, after, count, records = pyfltr.grep_.replacer.apply_replace_to_file(
                file,
                compiled,
                replacement,
                encoding=encoding,
            )
        except (UnicodeDecodeError, OSError):
            continue
        if count == 0:
            continue

        files_changed += 1
        total_replacements += count
        before_hash = pyfltr.grep_.replacer.compute_hash(before)
        after_hash = pyfltr.grep_.replacer.compute_hash(after)

        file_changes.append(
            ReplaceFileChangeModel(
                file=str(file),
                count=count,
                before_hash=before_hash,
                after_hash=after_hash,
            )
        )

        if show_changes:
            for record in records:
                change_records.append(
                    ReplaceChangeRecordModel(
                        file=str(record.file),
                        line=record.line,
                        col=record.col,
                        before_line=record.before_line,
                        after_line=record.after_line,
                    )
                )

        if not dry_run:
            file.write_text(after, encoding=encoding)
            history_entries.append(
                {
                    "file": file,
                    "before_content": before,
                    "after_hash": after_hash,
                    "records": list(records),
                }
            )

    # 実書き込み時に履歴を保存する
    if not dry_run and history_entries and replace_id is not None:
        meta = ReplaceCommandMeta(
            replace_id=replace_id,
            dry_run=False,
            fixed_strings=fixed_strings,
            pattern=pattern,
            replacement=replacement,
            encoding=encoding,
        )
        store = pyfltr.grep_.history.ReplaceHistoryStore()
        store.save_replace(replace_id, command_meta=meta, file_changes=history_entries)
        store.cleanup(pyfltr.grep_.history.policy_from_config(config))

    return ReplaceResultModel(
        replace_id=replace_id,
        dry_run=dry_run,
        files_changed=files_changed,
        total_replacements=total_replacements,
        file_changes=file_changes,
        changes=change_records,
        exit_code=0,
    )


async def _tool_replace_undo(replace_id: str, force: bool = False) -> ReplaceUndoModel:
    """保存済みreplace履歴からファイルを変更前の内容へ復元する。

    `force=True`を指定しない限り、手動編集済み（ハッシュ不一致）のファイルはスキップする。
    スキップが発生した場合は`exit_code=1`を返す。クライアント側で`force=True`再呼び出しの
    判断材料にする。

    Args:
        replace_id: undo対象のreplace識別子（ULID）。
        force: Trueの場合、ハッシュ不一致のファイルも強制復元する。
    """
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        restored, skipped = store.undo_replace(replace_id, force=force)
    except FileNotFoundError:
        _raise_mcp_error(f"replace_id が見つからない: {replace_id}")

    exit_code = 1 if skipped else 0
    return ReplaceUndoModel(
        replace_id=replace_id,
        restored=[str(p) for p in restored],
        skipped=[str(p) for p in skipped],
        exit_code=exit_code,
    )


def _has_hidden_segment(path: pathlib.Path) -> bool:
    """パス内に`.`始まりのセグメント（`.`/`..`を除く）が含まれるか判定する。"""
    for part in path.parts:
        if part in (".", ".."):
            continue
        if part.startswith("."):
            return True
    return False


# ---------------------------------------------------------------------------
# FastMCPサーバー組み立て
# ---------------------------------------------------------------------------


def _build_server() -> typing.Any:
    """FastMCPサーバーインスタンスを生成し、8ツールを登録して返す。

    公開名は`@mcp.tool(name=...)`で明示し、Python側の関数名（`_tool_*`）
    とは独立したスキーマ名（`list_runs`等）を維持する。
    戻り値型を`typing.Any`とするのは、`mcp`未インストール環境でも本モジュールの
    importが機能しなくならないよう`FastMCP`を本関数内で局所importする設計に合わせるため。
    """
    try:
        # オプショナル依存`mcp`が未導入の環境でも本モジュールがimportできるよう、
        # FastMCPはtry/except内で遅延importする（モジュールトップレベルへ昇格しない）。
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
    mcp.tool(
        name="grep",
        description=(
            "Search for a regex pattern across files. Honors pyfltr exclude/.gitignore by default. Returns match records."
        ),
    )(_tool_grep)
    mcp.tool(
        name="replace",
        description=(
            "Replace pattern with replacement across files."
            " dry_run=True (default) previews changes without writing."
            " Pass dry_run=False to write and save undo history."
        ),
    )(_tool_replace)
    mcp.tool(
        name="replace_undo",
        description=(
            "Undo a previous replace by replace_id."
            " Set force=True to override hash mismatch (when files were edited after the replace)."
        ),
    )(_tool_replace_undo)

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
    del args  # サブコマンド呼び出し規約上受け取るのみ（mcpは追加引数を持たない）

    # stdioトランスポートではstdoutをJSON-RPCフレームが専有するため、
    # ロギングは必ずstderrへ向ける。
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        server = _build_server()
        server.run(transport="stdio")
        return 0
    except Exception as e:  # MCPサーバー起動失敗をエージェント側へ非ゼロ終了で通知するため全例外を捕捉する
        logger.error("MCP サーバーの起動に失敗した: %s", e)
        return 1
