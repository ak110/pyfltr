"""LLM 向け JSON Lines 出力。

`--output-format=jsonl` で呼ばれ、CommandResult 群を LLM / エージェントが
読みやすいフラットな JSON Lines 形式 (header / diagnostic / tool / summary の 4 種別) に
変換して書き出す。

``diagnostic`` は `(tool, file)` 単位で集約し、個別指摘は ``messages[]`` に格納する。
ルールURLはtool単位の`hint-urls`辞書へ寄せることで、LLM入力時のトークン浪費を抑える。
"""

import importlib.metadata
import json
import logging
import os
import shlex
import sys
import threading
import typing

import pyfltr.cli  # pylint: disable=cyclic-import
import pyfltr.command
import pyfltr.config
import pyfltr.error_parser

logger = logging.getLogger(__name__)

# ストリーミング書き出し時に複数行（diagnostic行+tool行）をアトミックに出力するためのロック。
# 並列実行される linters/testers から同時にコールバックが呼ばれる可能性がある。
# 出力先は `pyfltr.cli.structured_logger` の handler に委ねるが、ログ 1 件 = 1 行の
# 粒度では複数行のグルーピングを保証できないためモジュール側でロックする。
_write_lock = threading.Lock()

_TRUNCATED_PREFIX = "... (truncated)\n"


def build_tool_lines(
    result: pyfltr.command.CommandResult,
    config: pyfltr.config.Config,
) -> list[str]:
    """1コマンド分のdiagnostic行+tool行をJSONL文字列のリストとして生成する。

    diagnostic行は ``(tool, file)`` 単位で集約され、個別指摘は ``messages[]`` に並ぶ。
    個別指摘の合計件数が ``jsonl-diagnostic-limit`` を超える場合は
    ``ErrorLocation`` 列を先頭 N 件で切ってから集約し、
    tool レコードに ``truncated.diagnostics_total`` を添付する。
    切り詰めは ``result.archived`` が True のときのみ適用し、False の場合は全件出力する
    (アーカイブから復元不能な情報欠落を防ぐため)。
    """
    sorted_errors = pyfltr.error_parser.sort_errors(result.errors, config.command_names)
    diagnostic_total = len(sorted_errors)
    diagnostic_limit = int(config.values.get("jsonl-diagnostic-limit", 0) or 0)

    diagnostics_truncated = False
    if 0 < diagnostic_limit < diagnostic_total and result.archived:
        sorted_errors = sorted_errors[:diagnostic_limit]
        diagnostics_truncated = True

    diagnostic_records, hint_urls = aggregate_diagnostics(sorted_errors)

    lines: list[str] = []
    for record in diagnostic_records:
        lines.append(_dump(record))
    lines.append(
        _dump(
            _build_tool_record(
                result,
                diagnostics=len(sorted_errors),
                diagnostic_total=diagnostic_total if diagnostics_truncated else None,
                config=config,
                hint_urls=hint_urls,
            )
        )
    )
    return lines


def aggregate_diagnostics(
    errors: typing.Iterable[pyfltr.error_parser.ErrorLocation],
) -> tuple[list[dict[str, typing.Any]], dict[str, str]]:
    """``ErrorLocation`` 列を ``(tool, file)`` 単位の集約dictへ変換する。

    戻り値:
        - ``diagnostic`` レコードのリスト。各要素は
          ``{"kind": "diagnostic", "tool": ..., "file": ..., "messages": [...]}``。
          ``messages`` は ``(line, col or 0, rule or "")`` 昇順で並ぶ。
        - rule→URL辞書（ハイフン区切りキー ``hint-urls`` として tool レコードに埋め込む用）。
          URLが生成できたruleのみ含む。

    同一ruleで異なるURLが現れた場合は最初に出たURLを採用し warning ログを残す。
    集約のキー順は入力順（``sort_errors()`` 済み）を尊重する。
    """
    groups: dict[tuple[str, str], list[pyfltr.error_parser.ErrorLocation]] = {}
    group_order: list[tuple[str, str]] = []
    hint_urls: dict[str, str] = {}
    for error in errors:
        key = (error.command, error.file)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(error)
        if error.rule is not None and error.rule_url is not None:
            existing = hint_urls.get(error.rule)
            if existing is None:
                hint_urls[error.rule] = error.rule_url
            elif existing != error.rule_url:
                logger.warning(
                    "aggregate_diagnostics: rule %r に複数のrule_urlが存在する。先勝ち採用: %s (後続 %s を無視)",
                    error.rule,
                    existing,
                    error.rule_url,
                )

    records: list[dict[str, typing.Any]] = []
    for key in group_order:
        command, file = key
        sorted_messages = sorted(
            groups[key],
            key=lambda e: (e.line, e.col if e.col is not None else 0, e.rule or ""),
        )
        records.append(
            {
                "kind": "diagnostic",
                "tool": command,
                "file": file,
                "messages": [_build_message_dict(e) for e in sorted_messages],
            }
        )
    return records, hint_urls


def build_lines(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    exit_code: int,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
) -> list[str]:
    """CommandResult群からJSONL各行を生成する。

    出力順:
        1. ``commands``と``files``が指定されていればkind="header"行
        2. ``warnings``が非空ならkind="warning"行
        3. ツール単位でdiagnostic行+tool行（``config.command_names``の定義順）
        4. summary行1行

    resultsは順序を問わない。内部で``config.command_names``順にソートする。
    ``warnings``は``pyfltr.warnings_.collected_warnings()``の返り値を想定する。
    ``run_id``が指定されていればheaderレコードに埋め込む。
    ``launcher_prefix``が指定されていれば``summary.guidance``内の起動コマンド表記に反映する。
    """
    ordered = sorted(results, key=lambda r: _command_index(config, r.command))

    lines: list[str] = []

    if commands is not None and files is not None:
        lines.append(_dump(_build_header_record(commands, files, run_id=run_id)))

    for warning in warnings or []:
        lines.append(_dump(_build_warning_record(warning)))

    for result in ordered:
        lines.extend(build_tool_lines(result, config))

    lines.append(_dump(_build_summary_record(ordered, exit_code=exit_code, run_id=run_id, launcher_prefix=launcher_prefix)))
    return lines


def _command_index(config: pyfltr.config.Config, command: str) -> int:
    """config.command_names 内での位置を返す (未登録コマンドは末尾扱い)。"""
    if command in config.command_names:
        return config.command_names.index(command)
    return len(config.command_names)


def write_jsonl_header(commands: list[str], files: int, *, run_id: str | None = None) -> None:
    """header行を構造化出力loggerに書き出す（ストリーミングモード用）。

    パイプライン開始直後、diagnostic行より前に1回だけ呼ぶ。``run_id``が指定されていれば
    headerレコードに含める (アーカイブ参照時の識別キー)。
    出力先は ``pyfltr.cli.configure_structured_output()`` が設定した handler に従う
    （stdout もしくは `--output-file` の FileHandler）。
    """
    with _write_lock:
        pyfltr.cli.structured_logger.info(_dump(_build_header_record(commands, files, run_id=run_id)))


def write_jsonl_streaming(
    result: pyfltr.command.CommandResult,
    config: pyfltr.config.Config,
) -> None:
    """1コマンド分のdiagnostic行+tool行を構造化出力loggerに即時書き出す。

    ``_write_lock``取得下で複数行を連続書き出しすることで、並列実行されるlinters/testers
    から呼ばれてもツール単位のグルーピングが崩れない。
    """
    lines = build_tool_lines(result, config)
    with _write_lock:
        for line in lines:
            pyfltr.cli.structured_logger.info(line)


def write_jsonl_footer(
    results: list[pyfltr.command.CommandResult],
    *,
    exit_code: int,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
) -> None:
    """warning行+summary行を構造化出力loggerに書き出す。

    ``results``は``_build_summary_record()``の集計に使用する。
    ``run_id``と``launcher_prefix``は``summary.guidance``の起動コマンド整形に使う。
    """
    with _write_lock:
        for warning in warnings or []:
            pyfltr.cli.structured_logger.info(_dump(_build_warning_record(warning)))
        pyfltr.cli.structured_logger.info(
            _dump(_build_summary_record(results, exit_code=exit_code, run_id=run_id, launcher_prefix=launcher_prefix))
        )


def _dump(record: dict[str, typing.Any]) -> str:
    """JSON 1 行にシリアライズする。ensure_ascii=False + 区切り最短化でトークン効率を稼ぐ。"""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


_SCHEMA_HINTS: dict[str, str] = {
    "diagnostic.messages": (
        "per (tool,file) array of individual findings sorted by line/col/rule;"
        " each item carries line/col/rule/severity/fix/msg (optional fields omitted when absent)"
    ),
    "diagnostic.messages.fix": (
        "safe/unsafe/suggested = auto-fixable; none = tool reports no auto-fix; omitted = no fix info from tool"
    ),
    "diagnostic.messages.severity": "error/warning/info normalised across tools; omitted when not reported",
    "tool.hint-urls": ("mapping of rule id to documentation URL for this tool; omitted when no rule URLs are available"),
    "tool.retry_command": ("shell command to re-run only this tool on failing files; populated only when the tool failed"),
    "tool.cached": "true = result restored from file-hash cache; rerun with --no-cache to force",
    "tool.truncated": ("diagnostics or message were trimmed; full content is in the archive directory (see header.run_id)"),
    "header.run_id": "ULID identifying this run; use 'pyfltr show-run <run_id>' to fetch full output",
    "warning.hint": (
        "optional short mitigation/fix suggestion for this specific warning; omitted when the source does not provide one"
    ),
}
"""JSONL 出力フィールドの意味を補足する英語ガイド。

LLM 入力として読まれる前提のため英語で記述する (トークン効率と汎用性)。
``header.schema_hints`` として毎回の run に同梱することで、LLM がこの情報を
事前知識として持たなくても JSONL を解釈できるようにする。
"""


def _build_header_record(
    commands: list[str],
    files: int,
    *,
    run_id: str | None = None,
) -> dict[str, typing.Any]:
    """実行環境の基本情報を header レコード dict として返す。"""
    record: dict[str, typing.Any] = {
        "kind": "header",
        "version": importlib.metadata.version("pyfltr"),
        "python": sys.version,
        "executable": sys.executable,
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "commands": commands,
        "files": files,
    }
    if run_id is not None:
        record["run_id"] = run_id
    # LLM 向けフィールド補足。毎回出力する (header は各 run の先頭 1 行のみ)。
    record["schema_hints"] = dict(_SCHEMA_HINTS)
    return record


def _build_warning_record(entry: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """警告 dict を warning レコード dict に変換する。"""
    record: dict[str, typing.Any] = {
        "kind": "warning",
        "source": entry["source"],
        "msg": entry["message"],
    }
    hint = entry.get("hint")
    if hint is not None:
        record["hint"] = hint
    return record


def _build_message_dict(error: pyfltr.error_parser.ErrorLocation) -> dict[str, typing.Any]:
    """ErrorLocation を集約 ``messages[]`` 要素の dict に変換する。

    フィールド順は ``line`` → ``col`` → ``rule`` → ``severity`` → ``fix`` → ``msg``。
    ``rule_url`` は含めず、tool レコードの ``hint-urls`` へ集約する。
    None のフィールドは出力しない（``msg`` は常に出力）。
    """
    record: dict[str, typing.Any] = {"line": error.line}
    if error.col is not None:
        record["col"] = error.col
    if error.rule is not None:
        record["rule"] = error.rule
    if error.severity is not None:
        record["severity"] = error.severity
    if error.fix is not None:
        record["fix"] = error.fix
    record["msg"] = error.message
    return record


def _build_tool_record(
    result: pyfltr.command.CommandResult,
    *,
    diagnostics: int,
    diagnostic_total: int | None = None,
    config: pyfltr.config.Config | None = None,
    hint_urls: dict[str, str] | None = None,
) -> dict[str, typing.Any]:
    """CommandResult を tool レコード dict に変換する。

    ``diagnostics`` は集約前の個別指摘件数 (messages合計) を指定する。
    ``failed`` かつ ``diagnostics == 0`` のときに限り、``CommandResult.output`` の末尾を
    ``_truncate_message()`` でトリムして ``message`` フィールドを付与する。
    メッセージ切り詰めまたは diagnostic 切り詰めが発生した場合は ``truncated`` メタを
    添付する。retry_command は ``CommandResult.retry_command`` が設定されていれば含める。
    ``hint_urls`` が非空なら ``hint-urls`` キー（ハイフン区切り）で埋め込む。
    """
    record: dict[str, typing.Any] = {
        "kind": "tool",
        "tool": result.command,
        "type": result.command_type,
        "status": result.status,
        "files": result.files,
        "elapsed": round(result.elapsed, 2),
        "diagnostics": diagnostics,
    }
    if result.returncode is not None:
        record["rc"] = result.returncode

    truncated: dict[str, typing.Any] = {}
    if diagnostic_total is not None and diagnostic_total > diagnostics:
        truncated["diagnostics_total"] = diagnostic_total
        truncated["archive"] = f"tools/{result.command}/diagnostics.jsonl"

    if result.status == "failed" and diagnostics == 0:
        message_max_lines, message_max_chars = _resolve_message_limits(config)
        message, msg_truncated = _truncate_message(
            result.output,
            max_lines=message_max_lines,
            max_chars=message_max_chars,
            archived=result.archived,
        )
        if message:
            record["message"] = message
        if msg_truncated:
            truncated["lines"] = len(result.output.splitlines())
            truncated["chars"] = len(result.output)
            truncated.setdefault("archive", f"tools/{result.command}/output.log")

    if truncated:
        record["truncated"] = truncated
    if result.retry_command is not None:
        record["retry_command"] = result.retry_command
    # ファイル hash キャッシュ (v3.0.0 パートD)。
    # ``cached=True`` のときはツール実行がスキップされ過去結果を復元したことを示す。
    # ``cached_from`` は復元元の run_id (ULID) で、show-run / MCP から全文参照できる。
    if result.cached:
        record["cached"] = True
        if result.cached_from is not None:
            record["cached_from"] = result.cached_from
    if hint_urls:
        record["hint-urls"] = dict(hint_urls)
    return record


def _resolve_message_limits(config: pyfltr.config.Config | None) -> tuple[int, int]:
    """tool.message の行数・文字数上限を config から取得する。

    設定未指定時はパートC 以前のハードコード値 (30 行 / 2000 文字) を踏襲する。
    """
    if config is None:
        return 30, 2000
    max_lines = int(config.values.get("jsonl-message-max-lines", 30) or 0)
    max_chars = int(config.values.get("jsonl-message-max-chars", 2000) or 0)
    return max_lines, max_chars


def _build_failure_guidance(run_id: str | None, launcher_prefix: list[str] | None) -> list[str]:
    """失敗時に LLM エージェントへ次の一手を示す英語ガイドを生成する。

    ``summary.guidance`` として ``failed > 0`` の場合にのみ同梱する (成功時は不要)。
    ``run_id``と``launcher_prefix``が指定されていれば、起動コマンド表記と実run_idを埋め込む。
    未指定時はプレースホルダ (``<run_id>``)・既定値 (``pyfltr``) にフォールバックする。
    """
    launcher = shlex.join(launcher_prefix) if launcher_prefix else "pyfltr"
    run_id_token = run_id if run_id is not None else "<run_id>"
    return [
        "Inspect tool.retry_command in failed tool records to re-run only failing files.",
        f"Use '{launcher} run-for-agent --only-failed' to retry the failure set in one step.",
        "diagnostic.fix == 'safe'/'unsafe'/'suggested' means the tool can auto-fix; 'none' or omitted means manual fix needed.",
        f"Use '{launcher} show-run {run_id_token}' for full per-tool output stored in the run archive.",
    ]


def _build_summary_record(
    ordered_results: list[pyfltr.command.CommandResult],
    *,
    exit_code: int,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
) -> dict[str, typing.Any]:
    """ordered_results から集計して summary レコード dict を作る。"""
    counts = {"succeeded": 0, "formatted": 0, "failed": 0, "skipped": 0}
    total_diagnostics = 0
    for result in ordered_results:
        counts[result.status] = counts.get(result.status, 0) + 1
        total_diagnostics += len(result.errors)
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "total": len(ordered_results),
        "succeeded": counts["succeeded"],
        "formatted": counts["formatted"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "diagnostics": total_diagnostics,
        "exit": exit_code,
    }
    if counts["failed"] > 0:
        record["guidance"] = _build_failure_guidance(run_id, launcher_prefix)
    return record


def _truncate_message(
    output: str,
    *,
    max_lines: int,
    max_chars: int,
    archived: bool,
) -> tuple[str, bool]:
    r"""生出力を指定上限にトリムする。トリム時は先頭に `"... (truncated)\n"` を付与する。

    戻り値は ``(切り詰め後メッセージ, 切り詰め発生したか)`` のタプル。空文字は
    ``("", False)`` を返す (呼び出し側で message キーごと省略する)。
    ``archived`` が ``False`` の場合は切り詰めを行わず全文を返す (アーカイブから
    復元不能な情報欠落を避けるため)。``max_lines`` / ``max_chars`` が 0 以下の場合も
    当該軸の切り詰めを行わない。
    """
    if not output:
        return "", False
    if not archived:
        return output, False
    lines = output.splitlines()
    truncated = False
    if 0 < max_lines < len(lines):
        lines = lines[-max_lines:]
        truncated = True
    body = "\n".join(lines)
    if 0 < max_chars < len(body):
        body = body[-max_chars:]
        truncated = True
    if truncated:
        return _TRUNCATED_PREFIX + body, True
    return body, False
