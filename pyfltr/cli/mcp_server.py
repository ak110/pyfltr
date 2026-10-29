"""MCPサーバー本体。

`pyfltr mcp`サブコマンドでstdioトランスポートのMCPサーバーを起動する。
MCPServerを用いて11ツールを公開し、
LLMエージェントがpyfltrの実行と実行アーカイブ参照を直接利用できるようにする。

MCPをコーディングエージェントの常用経路と位置づけ、CLIで可能な操作を原則として露出する。
端末表示と出力先の制御は、MCPが構造化データを戻り値で返すため対象外とする。
`--no-archive`も`run_id`を返す戻り値契約と両立しないため露出しない。
ビルトインツールごとの`--{tool}-args`群は、スキーマの肥大化に見合う用途がないため対象外とする。

引数解決はCLIと共通化する。サブコマンド既定値の注入、カンマ区切りの展開、
未知コマンドの検証には`pyfltr.cli.command_selection`の公開関数を用いる。

動作: `run`ツールは`pyfltr.cli.pipeline.run_pipeline`を直接呼ぶため、
モノレポモード（起点cwd配下に複数の`pyproject.toml`を検出した場合）の
サブプロジェクト分割実行をMCP経由でも自動的に継承する。
公開スキーマには`subproject`識別フィールドを追加しないため、利用者が観測する
レコード構造は単一プロジェクト時と同じになる。

サーバー実装を`mcp_server.py`へ分離し、サードパーティ`mcp`パッケージとの
import衝突を避けながらCLI入口から遅延なく参照できる構成にする。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import pathlib
import sys

# 本モジュールは`types`をgrep系ツール関数の引数名に使うため、標準ライブラリ側を別名で取り込む。
# 同名のままではモジュール名を引数が覆い、pylintの`redefined-outer-name`に抵触する。
import types as types_module
import typing

import pydantic

# 配布物の版指定は下流プロジェクトの依存解決で上書きされる場合がある。
# MCP専用依存のimport失敗を捕捉し、他サブコマンドとヘルプの起動を維持する。
try:
    import mcp.server.mcpserver as _imported_mcpserver
    import mcp.server.mcpserver.exceptions as _imported_mcp_exceptions
    import mcp.types as _imported_mcp_types

    import pyfltr.cli.mcp_transport as _imported_mcp_transport
except ImportError as e:  # 依存解決が配布物の宣言と異なる環境で到達する。
    _mcpserver: types_module.ModuleType | None = None
    _mcp_exceptions: types_module.ModuleType | None = None
    _mcp_types: types_module.ModuleType | None = None
    _mcp_transport: types_module.ModuleType | None = None
    _MCP_IMPORT_ERROR: ImportError | None = e
else:
    _mcpserver = _imported_mcpserver
    _mcp_exceptions = _imported_mcp_exceptions
    _mcp_types = _imported_mcp_types
    _mcp_transport = _imported_mcp_transport
    _MCP_IMPORT_ERROR = None

import pyfltr.cli.command_info
import pyfltr.cli.command_selection
import pyfltr.cli.pipeline
import pyfltr.command.core_
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.editing
import pyfltr.config.model
import pyfltr.config.operations
import pyfltr.config.selection
import pyfltr.config.validation
import pyfltr.grep_.adaptive
import pyfltr.grep_.history
import pyfltr.grep_.jsonl_records
import pyfltr.grep_.matcher
import pyfltr.grep_.operations
import pyfltr.grep_.preview
import pyfltr.grep_.replacer
import pyfltr.grep_.scanner
import pyfltr.output.jsonl
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.runs
import pyfltr.warnings_
from pyfltr.cli.mcp_models import (
    CommandDiagnosticsModel,
    CommandInfoModel,
    CommandMetaModel,
    CommandSummaryModel,
    ConfigResultModel,
    DiagnosticMessageModel,
    DiagnosticModel,
    GrepFileCountModel,
    GrepFileResultModel,
    GrepMatchModel,
    GrepResultModel,
    OutputRangeModel,
    ReplaceChangeRecordModel,
    ReplaceFileChangeModel,
    ReplaceHistoryEntryModel,
    ReplaceHistoryFileModel,
    ReplaceHistoryModel,
    ReplaceResultModel,
    ReplaceUndoModel,
    RunOverviewModel,
    RunResult,
    RunSummaryModel,
    RunWarningModel,
    SlowTestModel,
)

if typing.TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# エラー変換ヘルパー
# ---------------------------------------------------------------------------


def _raise_mcp_error(msg: str) -> typing.Never:
    """MCPクライアントへエラーとして返すための例外を送出する。

    想定内の失敗はmcp SDKの`ToolError`で送出する。
    MCPServerは`ToolError`だけを本文付きの`is_error`応答へ変換し、
    それ以外の例外（`ValueError`など）はクラッシュとして扱い、
    本文を`Error executing tool <ツール名>`へ置き換える（mcp 2.1.0以降）。
    """
    if _mcp_exceptions is None:
        # ツール関数はbuild_server経由で呼ばれ、mcpを読み込めない環境ではbuild_serverが先に停止する。
        raise RuntimeError("MCPサーバー機能に必要な依存を読み込めません") from _MCP_IMPORT_ERROR
    raise _mcp_exceptions.ToolError(msg)


def _resolve_run_id_or_raise(store: pyfltr.state.archive.ArchiveStore, raw: str) -> str:
    """`resolve_run_id`の結果を返し、エラー時はMCPエラーへ変換する。"""
    try:
        return pyfltr.state.runs.resolve_run_id(store, raw, list_runs="`list_runs`ツール", run="`run`ツール")
    except pyfltr.state.runs.RunIdError as e:
        _raise_mcp_error(str(e))


# ---------------------------------------------------------------------------
# 複数ツールが共有する引数の型
# ---------------------------------------------------------------------------

# `pydantic.Field`の説明はMCPの`inputSchema`へ載り、モデルが引数を決める唯一の手掛かりとなる。
# docstringはクライアントへ送られないため、意味・省略時の動作・組み合わせ条件は説明へ書く。

RunIdArg = typing.Annotated[
    str,
    pydantic.Field(
        description="対象runの識別子。ULIDの完全一致・前方一致と、最新のrunを指す`latest`を受け付ける。",
    ),
]
ArchivedCommandsArg = typing.Annotated[
    list[str],
    pydantic.Field(
        description=(
            "結果を取得するコマンド名の一覧（1件以上）。"
            "runで実行したコマンド名は`show_run`の`commands`で確認できる。結果はこの順に返す。"
        ),
    ),
]
IgnoreCaseArg = typing.Annotated[bool, pydantic.Field(description="Trueで大文字と小文字を区別しない。")]
SmartCaseArg = typing.Annotated[
    bool,
    pydantic.Field(description="Trueでパターンに大文字を含まない場合だけ大文字と小文字を区別しない。"),
]
FixedStringsArg = typing.Annotated[bool, pydantic.Field(description="Trueでパターンを正規表現ではなく固定文字列として扱う。")]
WordRegexpArg = typing.Annotated[bool, pydantic.Field(description="Trueで単語境界に囲まれた一致だけを採用する。")]
LineRegexpArg = typing.Annotated[bool, pydantic.Field(description="Trueで行全体に一致した場合だけ採用する。")]
MultilineArg = typing.Annotated[bool, pydantic.Field(description="Trueで改行をまたぐ一致を許可する。")]
TypesArg = typing.Annotated[
    list[str] | None,
    pydantic.Field(description='対象を言語タイプで限定する一覧（例: ["python", "ts"]）。省略時は限定しない。'),
]
GlobsArg = typing.Annotated[
    list[str] | None, pydantic.Field(description="対象をglobパターンで限定する一覧。省略時は限定しない。")
]
MaxFilesizeArg = typing.Annotated[
    int | None,
    pydantic.Field(description="走査するファイルサイズの上限（バイト）。超えるファイルは対象外。省略または0で上限なし。"),
]
NoExcludeArg = typing.Annotated[
    bool, pydantic.Field(description="Trueでpyfltr設定の`exclude`・`extend-exclude`による除外を無効にする。")
]
NoGitignoreArg = typing.Annotated[bool, pydantic.Field(description="Trueで`.gitignore`による除外を無効にする。")]


# ---------------------------------------------------------------------------
# MCPServerツール関数群（公開名は@mcp.tool(name=...)で明示）
# ---------------------------------------------------------------------------

# build_server()内で登録するため、ここではデコレーターを付けない。
# 公開名はbuild_server()で@mcp.tool(name="...")によって明示的に設定する。
# 公開名はアンダースコア区切り（`list_runs`等）を採用する。CLIサブコマンドの
# ハイフン形式（`list-runs`）とは異なるが、`@mcp.tool()`のスキーマ名規則上
# ハイフンは非推奨で互換性のあるMCPServer経路もアンダースコア前提のため。


async def tool_list_runs(
    limit: typing.Annotated[int, pydantic.Field(description="返すrunの最大件数。既定は20件。")] = 20,
) -> list[RunSummaryModel]:
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


async def tool_show_run(run_id: RunIdArg) -> RunOverviewModel:
    """指定runのmeta情報とコマンド別サマリを返す。

    対応CLI: `pyfltr show-run <run_id>`
    """
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    try:
        meta = store.read_meta(resolved)
    except FileNotFoundError:
        _raise_mcp_error(pyfltr.state.runs.format_run_not_found(resolved, list_runs="`list_runs`ツール"))
    command_summaries = pyfltr.state.runs.collect_tool_summaries(store, resolved)
    commands = [
        CommandSummaryModel(
            command=entry.get("command"),
            status=entry.get("status"),
            diagnostics=entry.get("diagnostics"),
            elapsed=entry.get("elapsed"),
            slow_tests=[SlowTestModel.model_validate(test) for test in entry.get("slow_tests", [])],
        )
        for entry in command_summaries
    ]
    return RunOverviewModel(run_id=resolved, meta=meta, commands=commands)


async def tool_show_run_diagnostics(
    run_id: RunIdArg,
    commands: ArchivedCommandsArg,
    offset: typing.Annotated[
        int | None,
        pydantic.Field(
            ge=0,
            description=(
                "返す最初の個別指摘の位置（0始まり）。個別指摘は`diagnostics[].messages`の要素で数える。"
                "`limit`だけを指定した場合は0から数える。前回の応答の`next_offset`を渡すと続きを取得できる。"
            ),
        ),
    ] = None,
    limit: typing.Annotated[
        int | None,
        pydantic.Field(ge=1, description="返す個別指摘の最大件数。省略時は`offset`以降の全件を返す。"),
    ] = None,
) -> list[CommandDiagnosticsModel]:
    """指定run・コマンドのdiagnosticsとコマンドのmeta情報を返す。

    `offset`と`limit`を省略すると全件を返し、応答は範囲指定の導入前と同じになる。
    どちらかを指定すると、個別指摘を元の順序で数えた範囲だけを`(command, file)`単位へ集約し直して返し、
    `total_diagnostics`と`next_offset`を加える。範囲は`commands`の各要素へ同じ値で適用する。

    `diagnostics`は`(command, file)`単位の集約形式で、個別指摘は`messages`に並ぶ。
    rule→URL辞書`hint_urls`とrule→ヒント辞書`hints`はtool.json由来でそのまま返す。
    `command_meta`はtool.jsonから`commandline`を除いた項目で、検査対象ファイルの引数列を含めない。
    完全な引数列は`pyfltr show-run <run_id> --commands=<name>`で取得する。
    `commands`に複数を指定すると、要素ごとの結果を入力順で返す。

    対応CLI: `pyfltr show-run <run_id> --commands <name1>,<name2>`
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    ranged = offset is not None or limit is not None
    start = offset or 0
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    results: list[CommandDiagnosticsModel] = []
    for command in commands:
        try:
            tool_meta = store.read_tool_meta(resolved, command)
            diagnostics_raw = store.read_tool_diagnostics(resolved, command)
        except FileNotFoundError:
            _raise_mcp_error(pyfltr.state.runs.format_tool_result_missing(resolved, command, show_run="`show_run`ツール"))
        hint_urls = tool_meta.get("hint_urls") if isinstance(tool_meta.get("hint_urls"), dict) else None
        hints = tool_meta.get("hints") if isinstance(tool_meta.get("hints"), dict) else None
        if not ranged:
            results.append(
                CommandDiagnosticsModel(
                    command_meta=CommandMetaModel.model_validate(tool_meta),
                    diagnostics=[_build_diagnostic(d, d.get("messages", [])) for d in diagnostics_raw],
                    hint_urls=hint_urls,
                    hints=hints,
                )
            )
            continue
        # 個別指摘を通し番号で数え、範囲内の指摘だけを元のファイル単位の集約へ戻す。
        total = sum(len(d.get("messages", [])) for d in diagnostics_raw)
        end = total if limit is None else min(start + limit, total)
        diagnostics: list[DiagnosticModel] = []
        position = 0
        for d in diagnostics_raw:
            messages = d.get("messages", [])
            selected = messages[max(start - position, 0) : max(end - position, 0)]
            position += len(messages)
            if selected:
                diagnostics.append(_build_diagnostic(d, selected))
        results.append(
            CommandDiagnosticsModel(
                command_meta=CommandMetaModel.model_validate(tool_meta),
                diagnostics=diagnostics,
                hint_urls=hint_urls,
                hints=hints,
                total_diagnostics=total,
                next_offset=end if end < total else None,
            )
        )
    return results


def _build_diagnostic(entry: dict[str, typing.Any], messages: list[dict[str, typing.Any]]) -> DiagnosticModel:
    """保存済みの集約診断1件を、指定した指摘だけを持つ`DiagnosticModel`へ変換する。"""
    return DiagnosticModel(
        command=entry.get("command", entry.get("tool")),
        file=entry.get("file"),
        messages=[DiagnosticMessageModel(**m) for m in messages],
    )


async def tool_show_run_output(
    run_id: RunIdArg,
    commands: ArchivedCommandsArg,
    offset: typing.Annotated[
        int | None,
        pydantic.Field(
            ge=0,
            description=(
                "返す最初の行の位置（0始まり）。`limit`だけを指定した場合は0から数える。"
                "前回の応答の`next_offset`を渡すと続きを取得できる。"
            ),
        ),
    ] = None,
    limit: typing.Annotated[
        int | None,
        pydantic.Field(ge=1, description="返す最大行数。省略時は`offset`以降の全行を返す。"),
    ] = None,
) -> dict[str, str | OutputRangeModel]:
    """指定run・コマンドのoutput.logを返す。

    戻り値はコマンド名をキーとし、`commands`の入力順に並ぶ辞書。
    `offset`と`limit`を省略すると値は全文の文字列で、範囲指定の導入前と同じになる。
    どちらかを指定すると値は`OutputRangeModel`となり、範囲は`commands`の各要素へ同じ値で適用する。

    対応CLI: `pyfltr show-run <run_id> --commands <name> --output`（単一指定のみ）
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    ranged = offset is not None or limit is not None
    start = offset or 0
    outputs: dict[str, str | OutputRangeModel] = {}
    for command in commands:
        try:
            text = store.read_tool_output(resolved, command)
        except FileNotFoundError:
            _raise_mcp_error(pyfltr.state.runs.format_tool_result_missing(resolved, command, show_run="`show_run`ツール"))
        if not ranged:
            outputs[command] = text
            continue
        lines = text.splitlines(keepends=True)
        selected = lines[start:] if limit is None else lines[start : start + limit]
        end = start + len(selected)
        outputs[command] = OutputRangeModel(
            text="".join(selected),
            offset=start,
            returned_lines=len(selected),
            total_lines=len(lines),
            next_offset=end if end < len(lines) else None,
        )
    return outputs


async def tool_run(
    paths: typing.Annotated[
        list[str],
        pydantic.Field(
            description="検査対象のファイルまたはディレクトリのパス一覧。相対パスは`work_dir`（省略時はサーバーのcwd）を基準に解決する。",
        ),
    ],
    mode: typing.Annotated[
        typing.Literal["run", "fast", "ci"],
        pydantic.Field(
            description=(
                "実行モード。`run`はfixステージを有効にし、formatterによる書き換えを成功扱いにする。"
                "`fast`は`fast`設定が有効なツールだけを`run`と同じ扱いで実行する。"
                "`ci`はfixステージを無効にし、formatterによる書き換えを失敗扱いにする。"
            ),
        ),
    ] = "run",
    commands: typing.Annotated[
        list[str] | None,
        pydantic.Field(
            description="実行するコマンド名またはエイリアス（`lint`など）の一覧。カンマ区切りも受け付ける。省略時は設定で有効な全コマンド。",
        ),
    ] = None,
    enable: typing.Annotated[
        list[str] | None,
        pydantic.Field(description="この実行に限り有効にするコマンド名の一覧。カンマ区切りも受け付ける。"),
    ] = None,
    disable: typing.Annotated[
        list[str] | None,
        pydantic.Field(description="この実行に限り無効にするコマンド名の一覧。カンマ区切りも受け付ける。"),
    ] = None,
    exclude_fence_under: typing.Annotated[
        list[str] | None,
        pydantic.Field(description="Markdownでフェンス内側を検査対象から除くH2見出しの一覧。"),
    ] = None,
    no_fix: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで`run`・`fast`のfixステージを抑止する（`ci`は元から無効）。"
                "通常ステージのformatterは対象ファイルを書き換え、既定で有効な`ruff-format-by-check`により"
                "`ruff-format`も整形する。書き換えを避ける場合は`commands`をlinterへ限定する。"
            ),
        ),
    ] = False,
    fail_fast: typing.Annotated[bool, pydantic.Field(description="Trueで1つのツールが失敗した時点で残りを打ち切る。")] = False,
    only_failed: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで直前run（`from_run`指定時はそのrun）の失敗ツールを、失敗ファイルに限定して再実行する。"
                "診断にファイルを持たない失敗ツールは現在の対象全体で再実行する。"
            ),
        ),
    ] = False,
    from_run: typing.Annotated[
        str | None,
        pydantic.Field(
            description="`only_failed`の参照runの識別子（前方一致・`latest`可）。`only_failed=True`のときだけ指定できる。",
        ),
    ] = None,
    changed_since: typing.Annotated[
        str | None,
        pydantic.Field(description="指定したgit参照（ブランチ名・commitなど）から変更されたファイルだけを対象にする。"),
    ] = None,
    work_dir: typing.Annotated[
        str | None,
        pydantic.Field(
            description=(
                "実行の起点ディレクトリ。設定探索と相対パス解決の基準を兼ねる。"
                "省略時はMCPサーバープロセスのcwd。指定時は診断のファイルパスを絶対パスで返す場合がある。"
            ),
        ),
    ] = None,
    allow_external_paths: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで実行起点の外側にあるパスを許可する。"
                "その場合は`work_dir`へ検査設定を持つプロジェクトのルートを指定する。"
                "検査設定を持たないディレクトリを起点にすると、適用される除外設定と検査対象の範囲が変わる。"
            ),
        ),
    ] = False,
    no_exclude: NoExcludeArg = False,
    no_gitignore: NoGitignoreArg = False,
    no_cache: typing.Annotated[
        bool, pydantic.Field(description="Trueでファイルハッシュによる結果キャッシュを使わない。")
    ] = False,
    human_readable: typing.Annotated[
        bool, pydantic.Field(description="Trueで対応ツールの機械可読出力を抑止し、人間向けの出力にする。")
    ] = False,
    shuffle: typing.Annotated[bool, pydantic.Field(description="Trueで対象ファイルの順序をシャッフルする。")] = False,
    exit_zero_even_if_formatted: typing.Annotated[
        bool, pydantic.Field(description="Trueでformatterによる書き換えだけなら終了コードを0にする。")
    ] = False,
    jobs: typing.Annotated[int | None, pydantic.Field(description="並列実行するツール数の上限。省略時は設定値を使う。")] = None,
) -> RunResult:
    """指定パスに対してlint/format/testを実行し、結果を返す。

    `run`・`fast`・`ci`の各実行モードをCLIと同じ既定値で扱う。
    実行アーカイブは常に有効化する。初期化に失敗した場合は`run_id=None`となり、
    `warnings`の`source="archive"`が理由と対処を示す。実行結果はアーカイブの成否によらず返す。
    `only_failed`による早期終了（直前runなし・失敗ツールなし・対象ファイル交差が空・
    `from_run`の解決失敗・アーカイブの読み取り失敗）の場合は`run_id=None`とし、
    実際の理由と対処を`skipped_reason`へ設定して返す。
    早期終了せず、一部の検査だけ直前runの失敗ファイルが対象と交差せず除外された場合と、
    `commands`へ指定した検査が設定で無効化されているために実行されなかった場合も、
    対象の検査名と理由を`skipped_reason`へ設定する。いずれも`run_pipeline`が発行する
    `source="only-failed"`・`source="commands"`の警告を入力とし、MCP側では再判定しない。
    実行中の警告は`warnings`へ、失敗時などの次の操作は`guidance`へ設定する。
    返却へ含めた警告はサーバーのstderrへ重ねて出力しない。

    結果は`completion`を最初に読む。`completed`以外なら`incomplete_commands`・
    `missing_targets`・`fully_excluded_files`・`skipped_reason`・`warnings`で未完了の理由を、
    `failed`が空でなければ`show_run_diagnostics`で診断を確認する。
    完了判定は`run_pipeline`が導出した値をそのまま返し、CLI JSONLの`summary`と同じ値になる。

    対応CLI: `pyfltr run` / `pyfltr fast` / `pyfltr ci`
    """
    # `run_pipeline`は警告を初期化しないため、対象の呼び出しが発行した警告だけを
    # `skipped_reason`の入力にできるよう、ここで蓄積を初期化する。
    pyfltr.warnings_.clear()

    # 返却へ含めた警告を配送済みとして扱い、サーバーのstderrへ重ねて出力しないよう、
    # 返却の組み立てまでを配送保留スコープに含める。例外で返却に至らなかった警告は
    # スコープの終了時にstderrへ通知される。
    with pyfltr.warnings_.defer_stderr():
        if mode not in ("run", "fast", "ci"):
            _raise_mcp_error("mode は run / fast / ci のいずれかを指定してください。")
        if from_run is not None and not only_failed:
            _raise_mcp_error("from_run は only_failed=True のときのみ指定できます。")

        work_dir_path = pathlib.Path(work_dir).expanduser().resolve() if work_dir is not None else None
        if work_dir_path is not None and not work_dir_path.is_dir():
            _raise_mcp_error(
                f"work_dir が存在するディレクトリではありません: {work_dir}。存在するディレクトリの絶対パスを指定してください。"
            )

        base = work_dir_path if work_dir_path is not None else pathlib.Path.cwd()
        targets = [path if (path := pathlib.Path(raw)).is_absolute() else base / path for raw in paths]

        args = pyfltr.run_options.RunOptions(
            targets=targets,
            # CLI経路（`--commands`はaction="append"）と同じ`list[str] | None`で保持する。
            commands=list(commands) if commands else None,
            enable=list(enable) if enable else None,
            disable=list(disable) if disable else None,
            exclude_fence_under=list(exclude_fence_under) if exclude_fence_under else None,
            no_fix=no_fix,
            fail_fast=fail_fast,
            only_failed=only_failed,
            from_run=from_run,
            changed_since=changed_since,
            no_archive=False,  # アーカイブ必須化のため明示的にFalse
            no_cache=no_cache,
            verbose=False,
            output_format="jsonl",
            output_file=None,  # 後で一時ファイルで上書きする
            ui=None,
            no_ui=True,
            no_clear=True,
            stream=False,
            shuffle=False,
            keep_ui=False,
            ci=mode == "ci",
            human_readable=human_readable,
            no_exclude=no_exclude,
            no_gitignore=no_gitignore,
            allow_external_paths=allow_external_paths,
            jobs=jobs,
            work_dir=work_dir_path,
            exit_zero_even_if_formatted=False,
            version=False,
            subcommand=mode,
            # MCPの戻り値は実行アーカイブから組み立てるためJSONL縮約の影響を受けない。
            # quiet=Trueはstderrへのprecommitガイダンス抑止のみに作用する。
            quiet=True,
        )
        pyfltr.cli.command_selection.apply_subcommand_defaults(args)
        args.shuffle = shuffle
        if exit_zero_even_if_formatted:
            args.exit_zero_even_if_formatted = True
        if no_fix:
            args.include_fix_stage = False

        try:
            config, commands_list = pyfltr.cli.pipeline.prepare_run(
                args, archive_required=True, list_commands=pyfltr.cli.command_selection.MCP_LIST_COMMANDS
            )
        except ValueError as exc:
            _raise_mcp_error(pyfltr.config.config.format_config_error(exc, retry="`run`ツール"))

        outcome = pyfltr.cli.pipeline.run_pipeline(
            args,
            commands_list,
            config,
            start_cwd=base,
            original_sys_args=args.retry_arguments(for_mcp=True),
            force_text_on_stderr=True,
            jsonl_warnings_reach_consumer=False,
            archive_required=True,
        )

        exit_code = outcome.exit_code
        run_id = outcome.run_id
        completion = outcome.completion
        completion_fields: dict[str, typing.Any] = {
            "completion": completion.completion,
            "files_reached": completion.files_reached,
            "completed_commands": list(completion.completed_commands),
            "incomplete_commands": list(completion.incomplete_commands),
            "missing_targets": list(completion.missing_targets),
            "fully_excluded_files": list(completion.fully_excluded_files),
        }

        commands_model = [
            CommandSummaryModel(
                command=result.command,
                status=result.status,
                diagnostics=len(result.errors),
                elapsed=result.elapsed,
                slow_tests=[SlowTestModel.model_validate(test.to_dict()) for test in result.slow_tests],
            )
            for result in outcome.results
        ]
        failed_commands = [result.command for result in outcome.results if result.failed]
        retry_commands = {result.command: result.retry_command for result in outcome.results if result.retry_command}

        warning_entries = pyfltr.warnings_.collected_warnings()
        pyfltr.warnings_.mark_delivered(warning_entries)
        guidance = pyfltr.output.jsonl.build_summary_guidance(
            failure_present=bool(failed_commands),
            resolution_failed_present=any(result.resolution_failed for result in outcome.results),
            applied_fixes_present=any(result.formatted for result in outcome.results),
            run_id=run_id,
            launcher_prefix=None,
            subcommand=mode,
            for_mcp=True,
        )
        return RunResult(
            run_id=run_id,
            exit_code=exit_code,
            **completion_fields,
            failed=failed_commands,
            commands=commands_model,
            skipped_reason=_join_warning_texts(warning_entries, sources=("only-failed", "commands")),
            retry_commands=retry_commands,
            warnings=_build_run_warnings(warning_entries),
            guidance=guidance,
        )


def _build_run_warnings(entries: list[dict[str, typing.Any]]) -> list[RunWarningModel]:
    """蓄積された警告を`RunResult.warnings`の要素へ変換する。"""
    return [
        RunWarningModel(source=str(entry["source"]), message=str(entry["message"]), hint=str(entry["hint"]))
        for entry in entries
    ]


def _join_warning_texts(entries: list[dict[str, typing.Any]], *, sources: tuple[str, ...]) -> str | None:
    """指定した発生源の警告を対処込みの文字列へ整形して連結する。該当が無ければNoneを返す。"""
    texts = [pyfltr.warnings_.format_warning_text(entry) for entry in entries if entry.get("source") in sources]
    return " / ".join(texts) or None


async def tool_grep(
    paths: typing.Annotated[list[str], pydantic.Field(description="検索対象のファイルまたはディレクトリのパス一覧。")],
    pattern: typing.Annotated[
        str | None,
        pydantic.Field(description="検索パターン（正規表現）。`patterns`または`pattern_file`だけで指定する場合は省略できる。"),
    ] = None,
    patterns: typing.Annotated[
        list[str] | None,
        pydantic.Field(
            description="追加の検索パターンの一覧。`pattern`・`pattern_file`と合わせ、いずれかに一致すれば採用する。"
        ),
    ] = None,
    pattern_file: typing.Annotated[str | None, pydantic.Field(description="1行に1パターンを書いたファイルのパス。")] = None,
    ignore_case: IgnoreCaseArg = False,
    smart_case: SmartCaseArg = False,
    fixed_strings: FixedStringsArg = False,
    word_regexp: WordRegexpArg = False,
    line_regexp: LineRegexpArg = False,
    multiline: MultilineArg = False,
    before_context: typing.Annotated[int, pydantic.Field(description="一致行の前に含める文脈の行数。")] = 0,
    after_context: typing.Annotated[int, pydantic.Field(description="一致行の後に含める文脈の行数。")] = 0,
    context: typing.Annotated[
        int | None,
        pydantic.Field(description="前後の文脈行数の一括指定。`before_context`・`after_context`のうち0の方向だけへ適用する。"),
    ] = None,
    max_count: typing.Annotated[
        int | None, pydantic.Field(description="ファイルごとの最大一致件数。省略または0で無制限。")
    ] = None,
    max_total: typing.Annotated[
        int | None,
        pydantic.Field(
            description='全体の最大一致件数。省略または0で無制限。`summary_mode="files_without_match"`とは正の値を併用できない。'
        ),
    ] = None,
    summary_mode: typing.Annotated[
        typing.Literal["files_with_matches", "count", "files_without_match"] | None,
        pydantic.Field(
            description=(
                "集計モード。指定時は`matches`を空で返し、`files_with_matches`は一致したファイル一覧、"
                "`count`はファイルごとの一致件数、`files_without_match`は一致しなかったファイル一覧を返す。"
            ),
        ),
    ] = None,
    types: TypesArg = None,
    globs: GlobsArg = None,
    encoding: typing.Annotated[str, pydantic.Field(description="ファイルを読む文字コード。")] = "utf-8",
    max_filesize: MaxFilesizeArg = None,
    max_preview_chars: typing.Annotated[
        int | None,
        pydantic.Field(description="返す本文1件あたりの最大文字数。省略時は200文字、0で無制限。"),
    ] = None,
    auto_summary: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで、件数上限・プレビュー上限・集計を指定していない大きな結果を約10,000文字へ縮約し、"
                "一致の多いファイルを件数表示へ切り替える。Falseで指定範囲の全一致を返す。"
            ),
        ),
    ] = True,
    no_exclude: NoExcludeArg = False,
    no_gitignore: NoGitignoreArg = False,
) -> GrepResultModel:
    """指定ファイル群から正規表現パターンを検索し、マッチ一覧を返す。

    pyfltrの`exclude`/`extend-exclude`/`respect-gitignore`設定を尊重する。
    通常検索でも未指定の`max_total`は走査を制限せず、全結果から返却形式を選択する。
    """
    # warnings_はモジュールグローバルに蓄積するため、リクエスト開始時に初期化する
    pyfltr.warnings_.clear()
    collected = ([pattern] if pattern is not None else []) + list(patterns or [])
    if pattern_file is not None:
        try:
            collected.extend(pyfltr.grep_.matcher.read_pattern_file(pathlib.Path(pattern_file)))
        except OSError as exc:
            _raise_mcp_error(
                f"パターンファイルの読み込みに失敗しました: {exc}。`pattern_file`のパスと読み取り権限を確認してください"
            )
    if not collected:
        _raise_mcp_error(
            "パターンが指定されていません。`pattern`・`patterns`・`pattern_file`のいずれかで検索パターンを指定してください"
        )
    valid_summary_modes = ("files_with_matches", "count", "files_without_match")
    if summary_mode is not None and summary_mode not in valid_summary_modes:
        _raise_mcp_error("summary_mode は files_with_matches / count / files_without_match のいずれかを指定してください。")
    if summary_mode == "files_without_match" and max_total is not None and max_total > 0:
        _raise_mcp_error(
            "summary_mode=files_without_match では max_total に正の値を指定できません。"
            "max_totalを省略するか0を指定してください。"
        )
    request = pyfltr.grep_.operations.GrepRequest(
        patterns=collected,
        summary_only=summary_mode is not None,
        max_count=max_count,
        max_total=max_total,
        max_preview_chars=max_preview_chars,
        auto_summary=auto_summary,
        output_format="mcp",
        full_text_hint="`max_preview_chars=0`",
        targets=pyfltr.grep_.operations.TargetRequest.from_paths(paths, types or [], globs or [], no_exclude, no_gitignore),
        **pyfltr.grep_.operations.PatternOptions.arguments(
            (fixed_strings, ignore_case, smart_case, word_regexp, line_regexp, multiline),
            (before_context, after_context, context),
            encoding,
            max_filesize,
        ),
    )
    try:
        operation = pyfltr.grep_.operations.execute_grep(request)
    except pyfltr.grep_.operations.TargetConfigError as exc:
        _raise_mcp_error(pyfltr.config.config.format_config_error(exc, retry="`grep`ツール"))
    except ValueError as exc:
        _raise_mcp_error(f"{exc}。パターンの該当位置を修正するか、固定文字列には fixed_strings=True を指定してください。")
    expanded = operation.expanded
    per_file_counts = operation.per_file_counts
    total_matches = operation.total_matches
    selection = operation.selection
    files_scanned = len(expanded)
    matches = [GrepMatchModel.model_validate(payload) for payload in selection.matches]
    adaptive_file_results = [GrepFileResultModel.model_validate(result) for result in selection.file_results]

    files_with_matches = (
        [pyfltr.paths.normalize_separators(file) for file in per_file_counts] if summary_mode == "files_with_matches" else []
    )
    file_counts = (
        [
            GrepFileCountModel(file=pyfltr.paths.normalize_separators(file), count=count)
            for file, count in per_file_counts.items()
        ]
        if summary_mode == "count"
        else []
    )
    files_without_match = (
        [pyfltr.paths.normalize_separators(file) for file in expanded if file not in per_file_counts]
        if summary_mode == "files_without_match"
        else []
    )
    return GrepResultModel(
        matches=matches,
        file_results=adaptive_file_results,
        output_mode=selection.output_mode,
        total_matches=total_matches,
        files_scanned=files_scanned,
        exit_code=0 if total_matches > 0 else 1,
        returned_matches=selection.returned_matches,
        omitted_matches=selection.omitted_matches,
        omitted_files=selection.omitted_files,
        guidance=(
            [
                "Narrow the search by file path, globs, or types to inspect omitted matches.",
                "Set auto_summary=false to return every match in the selected search range.",
            ]
            if selection.output_mode != "full"
            else []
        ),
        warnings=[pyfltr.warnings_.format_warning_text(entry) for entry in pyfltr.warnings_.collected_warnings()],
        fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
        missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
        summary_mode=summary_mode,
        files_with_matches=files_with_matches,
        file_counts=file_counts,
        files_without_match=files_without_match,
    )


async def tool_replace(
    pattern: typing.Annotated[str, pydantic.Field(description="置換対象を探すパターン（正規表現）。")],
    replacement: typing.Annotated[
        str, pydantic.Field(description="置換後の文字列。`re.sub`互換で、`\\1`や`\\g<name>`でグループを参照できる。")
    ],
    paths: typing.Annotated[
        list[str], pydantic.Field(description="置換対象のファイルまたはディレクトリのパス一覧（1件以上）。")
    ],
    dry_run: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "既定のTrueではファイルを変更せず変更内容だけを返す。"
                "Falseを明示した場合だけファイルへ書き込み、取り消し用の`replace_id`を返す。"
            ),
        ),
    ] = True,
    ignore_case: IgnoreCaseArg = False,
    smart_case: SmartCaseArg = False,
    fixed_strings: FixedStringsArg = False,
    word_regexp: WordRegexpArg = False,
    line_regexp: LineRegexpArg = False,
    multiline: typing.Annotated[
        bool, pydantic.Field(description="Trueで改行をまたぐ一致を許可する。`within`とは併用できない。")
    ] = False,
    within: typing.Annotated[
        str | None,
        pydantic.Field(
            description=(
                "置換する領域を決めるアンカーの正規表現。指定時はアンカーに一致した行と、"
                "その前後`before_context`・`after_context`行の範囲内に完全に収まる一致だけを置換する。"
            ),
        ),
    ] = None,
    before_context: typing.Annotated[
        int,
        pydantic.Field(description="`within`の領域でアンカー行の前に含める行数。`within`を指定した場合だけ使える。"),
    ] = 0,
    after_context: typing.Annotated[
        int,
        pydantic.Field(description="`within`の領域でアンカー行の後に含める行数。`within`を指定した場合だけ使える。"),
    ] = 0,
    context: typing.Annotated[
        int | None,
        pydantic.Field(description="`within`の領域の前後行数の一括指定。`within`を指定した場合だけ使える。"),
    ] = None,
    types: TypesArg = None,
    globs: GlobsArg = None,
    encoding: typing.Annotated[str, pydantic.Field(description="ファイルを読み書きする文字コード。")] = "utf-8",
    max_filesize: MaxFilesizeArg = None,
    exclude_files: typing.Annotated[
        list[str] | None, pydantic.Field(description="置換対象から除くファイルパスの一覧。")
    ] = None,
    from_grep: typing.Annotated[
        str | None,
        pydantic.Field(
            description=(
                "`pyfltr grep --output-format=jsonl`の出力を保存したファイルのパス。"
                "そこに現れるファイルへ対象を限定する。縮約された出力は拒否する。"
            ),
        ),
    ] = None,
    no_exclude: NoExcludeArg = False,
    no_gitignore: NoGitignoreArg = False,
    show_changes: typing.Annotated[
        bool, pydantic.Field(description="Trueで`changes`へ置換箇所ごとの変更前後の行を含める。")
    ] = False,
) -> ReplaceResultModel:
    r"""指定ファイル群へ正規表現置換を適用し、変更内容を返す。

    `dry_run=True`（既定）はファイルを変更せず変更内容のみを返す。
    `dry_run=False`を明示した場合のみ実書き込みし、`replace_id`を返す。
    `dry_run`の既定値がCLI（`False`）と異なるのはLLM暴発防止のため
    （`.claude/skills/grep-replace/SKILL.md`参照）。
    """
    if not paths:
        _raise_mcp_error("paths を 1 件以上指定してください。")

    # CLIの`-A`/`-B`/`-C`拒否方針と対称に、`within`なしのコンテキスト指定を拒否する。
    if within is None and (before_context or after_context or context is not None):
        _raise_mcp_error("before_context / after_context / context は within と併用してください。")
    # `within`は行範囲で領域を定めるためマルチラインとは併用不可。
    if within is not None and multiline:
        _raise_mcp_error("within と multiline は併用できません。withinを省略するかmultiline=Falseを指定してください。")

    pyfltr.warnings_.clear()
    request = pyfltr.grep_.operations.ReplaceRequest(
        pattern=pattern,
        replacement=replacement,
        dry_run=dry_run,
        within=within,
        exclude_files=[pathlib.Path(path) for path in exclude_files or []],
        from_grep=pathlib.Path(from_grep) if from_grep is not None else None,
        targets=pyfltr.grep_.operations.TargetRequest.from_paths(paths, types or [], globs or [], no_exclude, no_gitignore),
        **pyfltr.grep_.operations.PatternOptions.arguments(
            (fixed_strings, ignore_case, smart_case, word_regexp, line_regexp, multiline),
            (before_context, after_context, context),
            encoding,
            max_filesize,
        ),
    )
    try:
        operation = pyfltr.grep_.operations.execute_replace(request)
    except pyfltr.grep_.history.ReplaceFailure as exc:
        _raise_mcp_error(exc.describe(undo=f"`replace_undo(replace_id={exc.replace_id!r}, force=True)`"))
    except pyfltr.grep_.operations.TargetConfigError as exc:
        _raise_mcp_error(pyfltr.config.config.format_config_error(exc, retry="`replace`ツール"))
    except ValueError as exc:
        _raise_mcp_error(
            f"{exc}。正規表現の該当位置を修正するか、固定文字列には fixed_strings=True を指定してください。"
            "from_grepの読込失敗時は指定パスと読み取り権限を確認してください。"
        )
    replace_id = operation.replace_id
    file_changes = [
        ReplaceFileChangeModel(
            file=pyfltr.paths.normalize_separators(file),
            count=result.count,
            before_hash=pyfltr.grep_.replacer.compute_hash(result.before_content),
            after_hash=pyfltr.grep_.replacer.compute_hash(result.after_content),
        )
        for file, result in operation.prepared
    ]
    change_records = (
        [
            ReplaceChangeRecordModel(
                file=pyfltr.paths.normalize_separators(record.file),
                line=record.line,
                col=record.col,
                before_line=record.before_line,
                after_line=record.after_line,
            )
            for _file, result in operation.prepared
            for record in result.records
        ]
        if show_changes
        else []
    )
    files_changed = len(operation.prepared)
    total_replacements = sum(result.count for _file, result in operation.prepared)
    return ReplaceResultModel(
        replace_id=replace_id,
        dry_run=dry_run,
        files_changed=files_changed,
        total_replacements=total_replacements,
        file_changes=file_changes,
        changes=change_records,
        exit_code=0,
        fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
        missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
        warnings=[pyfltr.warnings_.format_warning_text(entry) for entry in pyfltr.warnings_.collected_warnings()],
    )


async def tool_replace_undo(
    replace_id: typing.Annotated[
        str,
        pydantic.Field(description="取り消すreplaceの識別子（ULID）。`replace`の応答か`replace_history`で確認できる。"),
    ],
    force: typing.Annotated[
        bool,
        pydantic.Field(description="Trueで、replace後に編集された（ハッシュが一致しない）ファイルも変更前の内容へ戻す。"),
    ] = False,
) -> ReplaceUndoModel:
    """保存済みreplace履歴からファイルを変更前の内容へ復元する。

    `force=True`を指定しない限り、手動編集済み（ハッシュ不一致）のファイルはスキップする。
    スキップが発生した場合は`exit_code=1`を返す。クライアント側で`force=True`再呼び出しの
    判断材料にする。
    """
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        restored, skipped, warnings = store.undo_replace(replace_id, force=force)
        warnings = [f"{warning}。復元後のファイルを読み直し、内容と改行の差分を確認してください。" for warning in warnings]
    except pyfltr.grep_.history.ReplaceFailure as exc:
        _raise_mcp_error(exc.describe(undo=f"`replace_undo(replace_id={replace_id!r}, force=True)`"))
    except FileNotFoundError:
        _raise_mcp_error(pyfltr.grep_.history.format_replace_id_not_found(replace_id, list_history="`replace_history`ツール"))
    except (UnicodeDecodeError, OSError) as exc:
        _raise_mcp_error(pyfltr.grep_.history.format_history_unreadable(replace_id, store.history_root / replace_id, exc))

    exit_code = 1 if skipped else 0
    if skipped:
        warnings = [*warnings, pyfltr.grep_.history.format_undo_skipped(len(skipped), force="`force=True`")]
    return ReplaceUndoModel(
        replace_id=replace_id,
        restored=[pyfltr.paths.normalize_separators(p) for p in restored],
        skipped=[pyfltr.paths.normalize_separators(p) for p in skipped],
        exit_code=exit_code,
        warnings=warnings,
    )


async def tool_replace_history(
    action: typing.Annotated[
        typing.Literal["list", "show"],
        pydantic.Field(description="`list`は履歴を新しい順に一覧し、`show`は`replace_id`の1件を返す。"),
    ] = "list",
    replace_id: typing.Annotated[str | None, pydantic.Field(description='`action="show"`で必須のreplace識別子。')] = None,
    limit: typing.Annotated[int | None, pydantic.Field(description='`action="list"`で返す最大件数。省略時は全件。')] = None,
) -> ReplaceHistoryModel:
    """replace履歴を一覧または単体で参照する。

    対応CLI: `pyfltr replace --list-history` / `pyfltr replace --show-history <ID>`
    """
    pyfltr.warnings_.clear()
    if action not in ("list", "show"):
        _raise_mcp_error("action は list / show のいずれかを指定してください。")
    if action == "show" and replace_id is None:
        _raise_mcp_error('action="show"では replace_id を指定してください。')

    store = pyfltr.grep_.history.ReplaceHistoryStore()
    if action == "list":
        raw_entries = store.list_replaces(limit=limit)
    else:
        try:
            raw_entries = [store.load_replace(typing.cast(str, replace_id))]
        except FileNotFoundError:
            _raise_mcp_error(
                pyfltr.grep_.history.format_replace_id_not_found(
                    typing.cast(str, replace_id), list_history='`replace_history`ツールの`action="list"`'
                )
            )

    entries = [
        ReplaceHistoryEntryModel(
            replace_id=str(entry["replace_id"]),
            saved_at=entry.get("saved_at"),
            command=dict(entry.get("command", {})),
            files=[
                ReplaceHistoryFileModel(
                    file=str(file_entry["file"]),
                    records_count=int(file_entry.get("records_count", 0)),
                )
                for file_entry in entry.get("files", [])
            ],
        )
        for entry in raw_entries
    ]
    return ReplaceHistoryModel(action=action, entries=entries)


async def tool_command_info(
    command: typing.Annotated[str, pydantic.Field(description="対象のコマンド名（`ruff-check`など）。")],
    check: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで実行時と同じ事前確認を行う。`mise install`・`mise trust`の試行や"
                "パッケージマネージャーの版確認などの副作用が起こりうる。既定はFalseで副作用を起こさない。"
            ),
        ),
    ] = False,
) -> CommandInfoModel:
    """ツールの起動方式の解決結果を返す。

    対応CLI: `pyfltr command-info <command> [--check]`
    """
    pyfltr.warnings_.clear()
    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        _raise_mcp_error(f"設定エラー: {pyfltr.config.config.format_config_error(exc, retry='`command_info`ツール')}")
    try:
        pyfltr.cli.command_selection.validate_commands(
            [command], config, list_commands=pyfltr.cli.command_selection.MCP_LIST_COMMANDS
        )
    except ValueError as exc:
        _raise_mcp_error(str(exc))
    info = pyfltr.cli.command_info.collect_info(command, config, do_check=check)
    return CommandInfoModel(command=command, resolved=bool(info.get("resolved", True)), info=info)


async def tool_config(
    action: typing.Annotated[
        typing.Literal["get", "set", "delete", "list"],
        pydantic.Field(
            description=(
                "操作。`get`は`key`の値を返す（`key`必須、`value`不可）。"
                "`set`は`key`へ`value`を書き込む（両方必須）。`delete`は`key`を削除する（`key`必須、`value`不可）。"
                "`list`は設定値を一覧する（`key`・`value`不可）。`set`・`delete`は設定ファイルを書き換える。"
            ),
        ),
    ],
    key: typing.Annotated[str | None, pydantic.Field(description="`get`・`set`・`delete`で必須の設定キー名。")] = None,
    value: typing.Annotated[str | None, pydantic.Field(description="`set`で必須の設定値。キーの型に合わせて変換する。")] = None,
    use_global: typing.Annotated[
        bool,
        pydantic.Field(description="Trueでグローバル設定ファイルを対象にする。既定はサーバーのcwdにある`pyproject.toml`。"),
    ] = False,
    include_defaults: typing.Annotated[
        bool, pydantic.Field(description='`action="list"`で、既定値のままのキーも含める。')
    ] = False,
) -> ConfigResultModel:
    """pyfltr設定ファイルを操作する。

    対応CLI: `pyfltr config get|set|delete|list`
    """
    pyfltr.warnings_.clear()
    if action not in ("get", "set", "delete", "list"):
        _raise_mcp_error("action は get / set / delete / list のいずれかを指定してください。")
    if action in ("get", "delete"):
        if key is None:
            _raise_mcp_error(f'action="{action}"では key を指定してください。')
        if value is not None:
            _raise_mcp_error(f'action="{action}"では value を指定できません。valueを省略して呼び直してください。')
    elif action == "set":
        if key is None or value is None:
            _raise_mcp_error('action="set"では key と value を指定してください。')
    elif key is not None or value is not None:
        _raise_mcp_error('action="list"では key と value を指定できません。keyとvalueを省略して呼び直してください。')
    if include_defaults and action != "list":
        _raise_mcp_error('include_defaults は action="list"のときのみ指定できます。')

    path = pyfltr.config.model.default_global_config_path() if use_global else pathlib.Path("pyproject.toml").absolute()
    result = pyfltr.config.operations.execute(
        pyfltr.config.operations.ConfigRequest(
            action=action,
            path=path,
            key=key,
            value=value,
            use_global=use_global,
            include_defaults=include_defaults,
            global_option="`use_global=True`",
            retry_operation="`config`ツール",
        )
    )
    if result.error is not None:
        _raise_mcp_error(result.error)
    if action == "get":
        return ConfigResultModel(action=action, path=str(path), key=key, value=result.value, is_default=result.is_default)
    if action == "set":
        return ConfigResultModel(action=action, path=str(path), key=key, value=result.value, warnings=result.warnings)
    if action == "delete":
        return ConfigResultModel(action=action, path=str(path), key=key, existed=result.existed)
    return ConfigResultModel(action=action, path=str(path), values=result.values)


# ---------------------------------------------------------------------------
# MCPServer組み立て
# ---------------------------------------------------------------------------

# 初期化応答でクライアントへ渡すサーバー全体の説明。ツール検索が有効なホストでは
# ツール名とこの説明だけが先に読まれるため、扱う作業とツールの選び方を書く。
SERVER_INSTRUCTIONS = (
    "pyfltrは多言語のformatter・linter・testerとプロジェクト固有のチェックを、設定に従って一括実行する。"
    "チェックの実行は`run`を使い、`mode`で`run`（自動修正あり）・`fast`（`fast`設定が有効なツールのみ）・`ci`（fixステージなし、formatterによる書き換えを失敗扱い）を選ぶ。"
    "実行結果は実行アーカイブに保存され、`list_runs`で一覧、`show_run`で概要、"
    "`show_run_diagnostics`で診断、`show_run_output`でツールの生出力を参照する。"
    "pyfltrの除外設定に従うファイル横断の検索は`grep`、置換は`replace`（既定はdry run）を使い、"
    "取り消しと履歴は`replace_undo`・`replace_history`で扱う。"
    "pyfltrの設定の参照と変更は`config`、各ツールの起動方式の確認は`command_info`を使う。"
)

TOOL_HANDLERS = {
    handler.__name__: handler
    for handler in (
        tool_list_runs,
        tool_show_run,
        tool_show_run_diagnostics,
        tool_show_run_output,
        tool_run,
        tool_grep,
        tool_replace,
        tool_replace_undo,
        tool_replace_history,
        tool_command_info,
        tool_config,
    )
}


def build_server() -> MCPServer:
    """MCPServerインスタンスを生成し、11ツールを登録して返す。

    公開名は`@mcp.tool(name=...)`で明示し、Python側の関数名（`tool_*`）
    とは独立したスキーマ名（`list_runs`等）を維持する。

    `version`は初期化応答の`serverInfo.version`としてクライアントへ返る
    MCPプロトコルの必須フィールドであり、省略するとSDKの既定値である空文字列が入る。
    値は`importlib.metadata`から取得し、JSONL headerレコード・SARIF出力と取得元を揃える
    （実行中の版と配布物の版を一致させるため）。
    """
    if _mcpserver is None:
        raise RuntimeError("MCPサーバー機能に必要な依存を読み込めません") from _MCP_IMPORT_ERROR
    assert _mcp_transport is not None and _mcp_types is not None
    mcp = _mcpserver.MCPServer("pyfltr", version=importlib.metadata.version("pyfltr"), instructions=SERVER_INSTRUCTIONS)
    read_only = _mcp_types.ToolAnnotations(read_only_hint=True)
    # 対象ファイルや設定ファイルを上書きしうるツール。ホストへ確認を促す。
    writes_files = _mcp_types.ToolAnnotations(destructive_hint=True)
    for name, title, description, tool_annotations, handler in (
        (
            "list_runs",
            "実行履歴の一覧",
            "実行アーカイブに保存されたrunの一覧を新しい順に返す。最新runだけを見る場合は`show_run`へ`latest`を渡す。",
            read_only,
            tool_list_runs,
        ),
        (
            "show_run",
            "実行結果の概要",
            "指定runのmeta情報とコマンド別サマリ（状態・診断件数・所要時間）を返す。"
            "診断の本文は`show_run_diagnostics`、ツールの生出力は`show_run_output`で取得する。",
            read_only,
            tool_show_run,
        ),
        (
            "show_run_diagnostics",
            "実行結果の診断",
            "指定run・コマンドの診断（ファイル単位に集約した指摘）とコマンドのmeta情報を返す。"
            "meta情報は検査対象ファイルの引数列を含まない。"
            "既定は全件を返す。件数が多い場合は`offset`・`limit`で個別指摘の範囲を指定して分割取得でき、"
            "応答の`total_diagnostics`と`next_offset`で続きを判断できる。",
            read_only,
            tool_show_run_diagnostics,
        ),
        (
            "show_run_output",
            "実行結果の出力ログ",
            "指定run・コマンドのツールの生出力（output.log）を返す。診断として解析された指摘は`show_run_diagnostics`で取得する。"
            "既定は全文を返す。出力が大きい場合は`offset`・`limit`で行範囲を指定して分割取得でき、"
            "各値が`text`・`total_lines`・`next_offset`を持つオブジェクトになる。",
            read_only,
            tool_show_run_output,
        ),
        (
            "run",
            "チェックの実行",
            "指定パスに対してformatter・linter・testerを実行し、完了判定・失敗コマンド・run_idなどを返す。"
            'formatterと`mode="run"`・`"fast"`のfixステージは対象ファイルを書き換える。'
            " 応答は`completion`を最初に読み、`completed`以外なら`incomplete_commands`・`missing_targets`・"
            "`fully_excluded_files`・`skipped_reason`・`warnings`で理由を確認する。"
            "`failed`が空でなければ`show_run_diagnostics`で診断を取得する。"
            " 戻り値の`retry_commands`は失敗コマンドを再実行するシェルコマンドで、"
            "`only_failed=True`でも失敗したツールだけを再実行できる。",
            writes_files,
            tool_run,
        ),
        (
            "grep",
            "ファイル横断の検索",
            "ファイル群から正規表現（`fixed_strings=True`で固定文字列）を検索し、一致一覧を返す。"
            "pyfltr設定の`exclude`と`.gitignore`に従って対象を選ぶ。ファイルは変更しない。"
            " 大きな結果は既定で縮約し、縮約時は`output_mode`と`omitted_matches`で省略を示す。"
            "置換は`replace`を使う。",
            read_only,
            tool_grep,
        ),
        (
            "replace",
            "ファイル横断の置換",
            "ファイル群へ正規表現置換を適用する。既定の`dry_run=True`では変更内容だけを返してファイルを変更しない。"
            "`dry_run=False`ではファイルを書き換え、取り消し用の`replace_id`と履歴を保存する。"
            "置換前の確認には`grep`、取り消しには`replace_undo`を使う。",
            writes_files,
            tool_replace,
        ),
        (
            "replace_undo",
            "置換の取り消し",
            "`replace`で書き換えたファイルを、`replace_id`の履歴から変更前の内容へ書き戻す。"
            "replace後に編集されたファイルは既定で書き戻さず`skipped`へ挙げ、`force=True`で強制的に書き戻す。",
            writes_files,
            tool_replace_undo,
        ),
        (
            "replace_history",
            "置換履歴の参照",
            '`replace`の履歴を一覧（`action="list"`）または1件（`action="show"`）で返す。ファイルは変更しない。',
            read_only,
            tool_replace_history,
        ),
        (
            "command_info",
            "ツール起動方式の確認",
            "コマンドの起動方式（runner・実行ファイル・最終コマンドラインなど）の解決結果を返す。"
            "既定は副作用を起こさない。`check=True`は`mise install`・`mise trust`の試行や版確認の副作用を伴う。",
            # `check=True`はmiseの導入・信頼設定を変えうるため読み取り専用とせず、上書きもしないため破壊的ともしない。
            _mcp_types.ToolAnnotations(read_only_hint=False, destructive_hint=False),
            tool_command_info,
        ),
        (
            "config",
            "設定の参照と変更",
            "pyfltrの設定を取得・一覧（`get`・`list`）し、変更・削除（`set`・`delete`）する。"
            "`set`・`delete`は`pyproject.toml`（`use_global=True`ではグローバル設定ファイル）を書き換える。",
            writes_files,
            tool_config,
        ),
    ):
        mcp.tool(name=name, title=title, description=description, annotations=tool_annotations)(
            _mcp_transport.isolate_tool(handler, _raise_mcp_error)
        )

    return mcp


# ---------------------------------------------------------------------------
# サブコマンド登録・エントリポイント
# ---------------------------------------------------------------------------


def execute_mcp(args: argparse.Namespace) -> int:
    """`mcp`サブコマンドの処理本体。

    stdioトランスポートでMCPサーバーを起動する。
    起動直後にroot loggerをstderrへ向けてJSON-RPCフレームのstdout汚染を防ぐ。
    MCPServerの`run(transport="stdio")`はstdin EOFで終了する。
    """
    del args  # サブコマンド呼び出し規約上受け取るのみ（mcpは追加引数を持たない）

    # stdioトランスポートではstdoutをJSON-RPCフレームが専有するため、
    # ロギングは必ずstderrへ向ける。
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")

    if _mcpserver is None:
        logger.error(
            "MCPサーバー機能に必要な依存が解決されていません。"
            "`uv tool install --reinstall pyfltr` でMCP用の環境を再導入し、"
            "クライアントのMCPサーバーを再起動してください: %s",
            _MCP_IMPORT_ERROR,
        )
        return 1

    try:
        server = build_server()
        server.run(transport="stdio")
        return 0
    except Exception as e:  # MCPサーバー起動失敗をエージェント側へ非ゼロ終了で通知するため全例外を捕捉する
        logger.error(
            "MCP サーバーの起動に失敗しました: %s。"
            "https://ak110.github.io/pyfltr/guide/troubleshooting/ のMCP関連の節で切り分け手順を確認してください",
            e,
        )
        return 1
