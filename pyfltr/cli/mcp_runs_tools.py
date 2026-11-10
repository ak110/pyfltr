"""実行履歴を取得するMCPツール。"""

from __future__ import annotations

import typing

import pydantic

import pyfltr.cli.mcp_errors
import pyfltr.state.archive
import pyfltr.state.runs
from pyfltr.cli.mcp_models import (
    CommandDiagnosticsModel,
    CommandMetaModel,
    CommandSummaryModel,
    DiagnosticMessageModel,
    DiagnosticModel,
    OutputRangeModel,
    RunOverviewModel,
    RunSummaryModel,
    RunTimingModel,
    SlowTestModel,
)


def _resolve_run_id_or_raise(store: pyfltr.state.archive.ArchiveStore, raw: str) -> str:
    """`resolve_run_id`の結果を返し、エラー時はMCPエラーへ変換する。"""
    try:
        return pyfltr.state.runs.resolve_run_id(store, raw, list_runs="`list_runs`ツール", run="`run`ツール")
    except pyfltr.state.runs.RunIdError as e:
        pyfltr.cli.mcp_errors.raise_mcp_error(str(e))


SHOW_RUN_META_KEYS: tuple[str, ...] = ("version", "cwd", "argv", "commands", "files", "exit_code", "started_at", "finished_at")
"""`show_run`の`meta`へ返す保存項目。

エージェントがrunの対象・起動引数・結果・時刻を判断に使う項目だけを返す。
`run_id`は応答の最上位、`slot_waits`は`timing.slot_waits`と重複し、`diagnostic_paths`は保存形式の版の判別印、
`python`・`executable`・`platform`は同じホストで動くエージェントの判断・操作に使わないため返さない。
未知のキー（別の版が保存した項目）も返さない。項目を加える場合は受信主体の判断・操作を確かめる。
"""


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
    """指定runのmeta情報、経過時間の要約（待機区間・run全体の経過）、コマンド別サマリを返す。

    対応CLI: `pyfltr show-run <run_id>`
    """
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    try:
        meta = store.read_meta(resolved)
    except FileNotFoundError:
        pyfltr.cli.mcp_errors.raise_mcp_error(pyfltr.state.runs.format_run_not_found(resolved, list_runs="`list_runs`ツール"))
    command_summaries = pyfltr.state.runs.collect_tool_summaries(store, resolved)
    commands = [
        CommandSummaryModel(
            command=entry.get("command"),
            status=entry.get("status"),
            diagnostics=entry.get("diagnostics"),
            elapsed=entry.get("elapsed"),
            started_at=entry.get("started_at"),
            finished_at=entry.get("finished_at"),
            slow_tests=[SlowTestModel.model_validate(test) for test in entry.get("slow_tests", [])],
        )
        for entry in command_summaries
    ]
    timing = RunTimingModel.model_validate(pyfltr.state.runs.summarize_timing(meta))
    public_meta = {key: meta[key] for key in SHOW_RUN_META_KEYS if key in meta}
    return RunOverviewModel(run_id=resolved, meta=public_meta, timing=timing, commands=commands)


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
        pyfltr.cli.mcp_errors.raise_mcp_error("commands を 1 件以上指定してください。")
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
            pyfltr.cli.mcp_errors.raise_mcp_error(
                pyfltr.state.runs.format_tool_result_missing(resolved, command, show_run="`show_run`ツール")
            )
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
        pyfltr.cli.mcp_errors.raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    ranged = offset is not None or limit is not None
    start = offset or 0
    outputs: dict[str, str | OutputRangeModel] = {}
    for command in commands:
        try:
            text = store.read_tool_output(resolved, command)
        except FileNotFoundError:
            pyfltr.cli.mcp_errors.raise_mcp_error(
                pyfltr.state.runs.format_tool_result_missing(resolved, command, show_run="`show_run`ツール")
            )
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
