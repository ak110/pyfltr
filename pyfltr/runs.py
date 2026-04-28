"""`list-runs` / `show-run` サブコマンドの実装。

実行アーカイブ（`archive.py`）に保存されたrunの読み取り経路をCLIから提供する。
パートFで追加予定のMCPサーバーでも本モジュールの読み取り処理を再利用する想定。

サブパーサー登録は`register_subparsers()`、処理本体は`execute_list_runs()` /
`execute_show_run()`が担う。`main.py`からは引数パース済みの`argparse.Namespace`
を受け取り、終了コードを返すだけの薄いAPIにする。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import typing

import pyfltr.archive

_OUTPUT_FORMATS: tuple[str, ...] = ("text", "json", "jsonl")
_DEFAULT_LIST_LIMIT: int = 20
"""既定の表示件数。画面1ページに収まる件数を目安に20件。"""


def register_subparsers(subparsers: typing.Any) -> None:
    """`list-runs` / `show-run` サブパーサーを登録する。

    `subparsers`は`ArgumentParser.add_subparsers()`の戻り値
    （`argparse._SubParsersAction`）を想定する。サブコマンド固有引数のみを
    登録し、実行系と共通の`--verbose`等は継承しない（参照系のため不要）。
    """
    lr = subparsers.add_parser(
        "list-runs",
        help="実行アーカイブ内の run 一覧を表示する。",
    )
    lr.add_argument(
        "--limit",
        type=int,
        default=_DEFAULT_LIST_LIMIT,
        help=f"表示する最大件数 (既定: {_DEFAULT_LIST_LIMIT})。",
    )
    lr.add_argument(
        "--output-format",
        choices=_OUTPUT_FORMATS,
        default="text",
        help="出力形式を指定する (既定: text)。",
    )

    sr = subparsers.add_parser(
        "show-run",
        help="指定 run の詳細を表示する。",
    )
    sr.add_argument(
        "run_id",
        help="表示対象の run_id。前方一致または 'latest' 指定可。",
    )
    sr.add_argument(
        "--commands",
        default=None,
        help="特定ツールに絞り込んで diagnostics を全件表示する。カンマ区切りで複数指定可。",
    )
    sr.add_argument(
        "--output",
        default=False,
        action="store_true",
        help="指定ツールの生出力 (output.log) 全文を表示する。--commands と併用する (単一指定のみ可)。",
    )
    sr.add_argument(
        "--output-format",
        choices=_OUTPUT_FORMATS,
        default="text",
        help="出力形式を指定する (既定: text)。",
    )


def execute_list_runs(args: argparse.Namespace) -> int:
    """`list-runs` サブコマンドの処理本体。"""
    output_format: str = args.output_format
    with _stdout_owned(output_format):
        store = pyfltr.archive.ArchiveStore()
        summaries = store.list_runs(limit=args.limit)
        if output_format == "text":
            _print_list_runs_text(summaries)
        elif output_format == "json":
            _print_json({"runs": [_summary_to_dict(s) for s in summaries]})
        else:
            for summary in summaries:
                _print_jsonl_line({"kind": "run", **_summary_to_dict(summary)})
    return 0


def execute_show_run(args: argparse.Namespace) -> int:
    """`show-run` サブコマンドの処理本体。"""
    output_format: str = args.output_format
    raw_run_id: str = args.run_id
    commands_arg: str | None = args.commands
    output_mode: bool = args.output

    tools: list[str] = []
    if commands_arg:
        tools = [t.strip() for t in commands_arg.split(",") if t.strip()]

    if output_mode and not tools:
        sys.stderr.write("エラー: --output は --commands と併用する必要がある。\n")
        return 1
    if output_mode and len(tools) > 1:
        sys.stderr.write("エラー: --output は --commands に単一ツール指定のみ許可する。\n")
        return 1

    with _stdout_owned(output_format):
        store = pyfltr.archive.ArchiveStore()
        try:
            run_id = resolve_run_id(store, raw_run_id)
        except RunIdError as e:
            sys.stderr.write(f"エラー: {e}\n")
            return 1

        try:
            meta = store.read_meta(run_id)
        except FileNotFoundError:
            sys.stderr.write(f"エラー: run_id が見つからない: {run_id}\n")
            return 1

        if output_mode and tools:
            return _show_tool_output(store, run_id, tools[0], output_format)
        if tools:
            return _show_tools_detail(store, run_id, tools, output_format)
        return _show_run_overview(store, run_id, meta, output_format)


class RunIdError(Exception):
    """run_id解決に失敗した際の例外。"""


def resolve_run_id(store: pyfltr.archive.ArchiveStore, raw: str) -> str:
    """run_id指定を解決する。

    `latest`エイリアス → 完全一致 → 前方一致の順に試す。前方一致が複数
    該当した場合は曖昧と判定してエラーとする。

    完全一致のみ受け付ける案は不採用。ULID 26文字を毎回手入力させるUXが
    現実的でなく、CLIからの`show-run` / `--from-run`利用とMCP経路の
    どちらでも先頭数文字での参照ニーズが強いため、前方一致と`latest`
    エイリアスを許容する。曖昧時はエラーで明示することで、誤ったrunの
    閲覧・再実行を防ぐ。
    """
    run_ids = [s.run_id for s in store.list_runs()]
    if raw == "latest":
        if not run_ids:
            raise RunIdError("アーカイブに run が存在しない。")
        return run_ids[0]
    if raw in run_ids:
        return raw
    matched = [rid for rid in run_ids if rid.startswith(raw)]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        sample = ", ".join(matched[:3])
        suffix = "..." if len(matched) > 3 else ""
        raise RunIdError(f"run_id のプレフィックスが曖昧: {raw!r} に {len(matched)} 件該当 ({sample}{suffix})")
    raise RunIdError(f"run_id が見つからない: {raw!r}")


def _summary_to_dict(summary: pyfltr.archive.RunSummary) -> dict[str, typing.Any]:
    """`RunSummary`を出力用dictに変換する。"""
    return {
        "run_id": summary.run_id,
        "started_at": summary.started_at,
        "finished_at": summary.finished_at,
        "exit_code": summary.exit_code,
        "commands": list(summary.commands),
        "files": summary.files,
    }


def _print_list_runs_text(summaries: list[pyfltr.archive.RunSummary]) -> None:
    """`list-runs` の text 出力（固定幅テーブル）。"""
    if not summaries:
        print("(no runs)")
        return
    header = ("RUN_ID", "STARTED_AT", "EXIT", "FILES", "COMMANDS")
    rows: list[tuple[str, ...]] = [header]
    for summary in summaries:
        rows.append(
            (
                summary.run_id,
                summary.started_at or "-",
                "-" if summary.exit_code is None else str(summary.exit_code),
                "-" if summary.files is None else str(summary.files),
                ",".join(summary.commands),
            )
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    last = len(widths) - 1
    for row in rows:
        cells = [value.ljust(widths[i]) if i < last else value for i, value in enumerate(row)]
        print("  ".join(cells))


def _show_run_overview(
    store: pyfltr.archive.ArchiveStore,
    run_id: str,
    meta: dict[str, typing.Any],
    output_format: str,
) -> int:
    """既定モード: meta + ツール別サマリを表示する。"""
    tool_summaries = _collect_tool_summaries(store, run_id)
    if output_format == "text":
        _print_run_overview_text(run_id, meta, tool_summaries)
    elif output_format == "json":
        _print_json({"run_id": run_id, "meta": meta, "commands": tool_summaries})
    else:
        _print_jsonl_line({"kind": "meta", **meta})
        for tool_summary in tool_summaries:
            _print_jsonl_line({"kind": "command", **tool_summary})
    return 0


def _collect_tool_summaries(
    store: pyfltr.archive.ArchiveStore,
    run_id: str,
) -> list[dict[str, typing.Any]]:
    """`tools/`配下から各ツールの要約（status / has_error / diagnostics）を集める。"""
    summaries: list[dict[str, typing.Any]] = []
    for tool in store.list_tools(run_id):
        try:
            tool_meta = store.read_tool_meta(run_id, tool)
        except FileNotFoundError:
            continue
        summaries.append(
            {
                "command": tool_meta.get("command", tool_meta.get("tool", tool)),
                "status": tool_meta.get("status"),
                "has_error": tool_meta.get("has_error"),
                "diagnostics": tool_meta.get("diagnostics"),
            }
        )
    return summaries


def _print_run_overview_text(
    run_id: str,
    meta: dict[str, typing.Any],
    tool_summaries: list[dict[str, typing.Any]],
) -> None:
    """`show-run`既定モードのtext出力（行形式`キー: 値`）。"""
    print(f"run_id: {run_id}")
    for key in ("started_at", "finished_at", "exit_code", "files", "cwd"):
        if key in meta and meta[key] is not None:
            print(f"{key}: {meta[key]}")
    commands = meta.get("commands") or []
    if commands:
        print(f"commands: {','.join(commands)}")
    print("")
    print("commands:")
    if not tool_summaries:
        print("  (no archived commands)")
        return
    for entry in tool_summaries:
        print(
            f"  {entry['command']}: "
            f"status={entry.get('status')} "
            f"has_error={entry.get('has_error')} "
            f"diagnostics={entry.get('diagnostics')}"
        )


def _show_tools_detail(
    store: pyfltr.archive.ArchiveStore,
    run_id: str,
    tools: list[str],
    output_format: str,
) -> int:
    """`--commands`モード: 指定ツールのtool.json + diagnostics.jsonlを表示する。

    複数ツール指定時は順に表示する。jsonモードでは`commands`配列にまとめる。
    """
    entries: list[tuple[str, dict[str, typing.Any], list[dict[str, typing.Any]]]] = []
    for tool in tools:
        try:
            tool_meta = store.read_tool_meta(run_id, tool)
            diagnostics = store.read_tool_diagnostics(run_id, tool)
        except FileNotFoundError:
            sys.stderr.write(f"エラー: run {run_id} にツール {tool!r} の結果が保存されていない。\n")
            return 1
        entries.append((tool, tool_meta, diagnostics))

    if output_format == "text":
        for index, (_tool, tool_meta, diagnostics) in enumerate(entries):
            if index > 0:
                print("")
            _print_tool_detail_text(tool_meta, diagnostics)
    elif output_format == "json":
        if len(entries) == 1:
            _, tool_meta, diagnostics = entries[0]
            _print_json({"command": tool_meta, "diagnostics": diagnostics})
        else:
            _print_json(
                {"commands": [{"command": tool_meta, "diagnostics": diagnostics} for _tool, tool_meta, diagnostics in entries]}
            )
    else:
        for _tool, tool_meta, diagnostics in entries:
            _print_jsonl_line({"kind": "command", **tool_meta})
            for diagnostic in diagnostics:
                # diagnostics.jsonl側はkind="diagnostic"込みで保存されているが、
                # 古いrunや外部書き出し経路を考慮してkindを明示的に埋める。
                record = {"kind": "diagnostic", **diagnostic}
                _print_jsonl_line(record)
    return 0


def _print_tool_detail_text(
    tool_meta: dict[str, typing.Any],
    diagnostics: list[dict[str, typing.Any]],
) -> None:
    """`--tool`モードのtext出力。

    `diagnostics`は`(command, file)`単位の集約形式を想定し、各file見出しの下に
    `messages[]`内の個別指摘をインデント付きで並べる。
    """
    for key in ("command", "type", "status", "returncode", "files", "elapsed", "diagnostics", "has_error"):
        if key in tool_meta and tool_meta[key] is not None:
            print(f"{key}: {tool_meta[key]}")
    if tool_meta.get("commandline"):
        print(f"commandline: {tool_meta['commandline']}")
    hint_urls = tool_meta.get("hint_urls")
    if isinstance(hint_urls, dict) and hint_urls:
        print("hint_urls:")
        for rule, url in hint_urls.items():
            print(f"  {rule}: {url}")
    print("")
    print("diagnostics:")
    if not diagnostics:
        print("  (none)")
        return
    for diagnostic in diagnostics:
        file_part = diagnostic.get("file") or "-"
        print(f"  {file_part}")
        messages = diagnostic.get("messages") or []
        for message in messages:
            print(f"    {_format_message_line(file_part, message)}")


def _format_message_line(file_part: str, message: dict[str, typing.Any]) -> str:
    """1件分のmessageを`file:line:col [severity] (rule) msg`形式に整形する。"""
    line = message.get("line")
    col = message.get("col")
    location = file_part
    if line is not None:
        location = f"{location}:{line}"
        if col is not None:
            location = f"{location}:{col}"
    severity = message.get("severity")
    rule = message.get("rule")
    msg = message.get("msg") or ""
    parts = [location]
    if severity:
        parts.append(f"[{severity}]")
    if rule:
        parts.append(f"({rule})")
    parts.append(msg)
    return " ".join(parts)


def _show_tool_output(
    store: pyfltr.archive.ArchiveStore,
    run_id: str,
    tool: str,
    output_format: str,
) -> int:
    """`--tool <name> --output`モード: output.log全文を表示する。"""
    try:
        output = store.read_tool_output(run_id, tool)
    except FileNotFoundError:
        sys.stderr.write(f"エラー: run {run_id} にツール {tool!r} の結果が保存されていない。\n")
        return 1
    if output_format == "text":
        sys.stdout.write(output)
        if output and not output.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    elif output_format == "json":
        _print_json({"command": tool, "output": output})
    else:
        _print_jsonl_line({"kind": "output", "command": tool, "content": output})
    return 0


def _print_json(obj: dict[str, typing.Any]) -> None:
    """単発JSONを整形付きでstdoutに書く（jsonモード）。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _print_jsonl_line(obj: dict[str, typing.Any]) -> None:
    """JSONを1行でstdoutに書く（jsonlモード）。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    sys.stdout.flush()


class _stdout_owned:  # noqa: N801  # pylint: disable=invalid-name
    """json / jsonlモード時にstdoutを構造化出力で専有するためのコンテキスト。

    root loggerを抑止してlogging経由のstdout/stderr混入を防ぐ。エラー出力は
    引き続き`sys.stderr.write()`で直接書く前提。textモードでは何もしない。
    """

    def __init__(self, output_format: str) -> None:
        self._active = output_format in ("json", "jsonl")
        self._saved: tuple[list[logging.Handler], int] | None = None

    def __enter__(self) -> None:
        if not self._active:
            return
        root = logging.getLogger()
        self._saved = (root.handlers[:], root.level)
        root.handlers.clear()
        root.setLevel(logging.CRITICAL + 1)

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        if self._saved is None:
            return
        handlers, level = self._saved
        root = logging.getLogger()
        root.handlers[:] = handlers
        root.setLevel(level)
        self._saved = None
