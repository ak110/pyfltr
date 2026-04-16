"""LLM 向け JSON Lines 出力。

`--output-format=jsonl` で呼ばれ、CommandResult 群を LLM / エージェントが
読みやすいフラットな JSON Lines 形式 (header / diagnostic / tool / summary の 4 種別) に
変換して書き出す。
"""

import importlib.metadata
import json
import os
import pathlib
import sys
import threading
import typing

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser

# ストリーミング書き出し時に複数行（diagnostic行+tool行）をアトミックに出力するためのロック。
# 並列実行される linters/testers から同時にコールバックが呼ばれる可能性がある。
_write_lock = threading.Lock()

# failed かつ diagnostics=0 のときに tool.message として載せる生出力のトリム上限。
# 末尾 30 行を取り出し、さらに末尾 2000 文字に切り詰める。
_MESSAGE_MAX_LINES = 30
_MESSAGE_MAX_CHARS = 2000
_TRUNCATED_PREFIX = "... (truncated)\n"


def build_tool_lines(
    result: pyfltr.command.CommandResult,
    config: pyfltr.config.Config,
) -> list[str]:
    """1コマンド分のdiagnostic行+tool行をJSONL文字列のリストとして生成する。

    diagnostic行はツール内でソートされる。
    """
    sorted_errors = pyfltr.error_parser.sort_errors(result.errors, config.command_names)
    lines: list[str] = []
    for error in sorted_errors:
        lines.append(_dump(_build_diagnostic_record(error)))
    lines.append(_dump(_build_tool_record(result, diagnostics=len(result.errors))))
    return lines


def build_lines(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    exit_code: int,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
) -> list[str]:
    """CommandResult群からJSONL各行を生成する。

    出力順:
        1. ``commands``と``files``が指定されていればkind="header"行
        2. ``warnings``が非空ならkind="warning"行
        3. ツール単位でdiagnostic行+tool行（``config.command_names``の定義順）
        4. summary行1行

    resultsは順序を問わない。内部で``config.command_names``順にソートする。
    ``warnings``は``pyfltr.warnings_.collected_warnings()``の返り値を想定する。
    """
    ordered = sorted(results, key=lambda r: _command_index(config, r.command))

    lines: list[str] = []

    if commands is not None and files is not None:
        lines.append(_dump(_build_header_record(commands, files)))

    for warning in warnings or []:
        lines.append(_dump(_build_warning_record(warning)))

    for result in ordered:
        lines.extend(build_tool_lines(result, config))

    lines.append(_dump(_build_summary_record(ordered, exit_code=exit_code)))
    return lines


def _command_index(config: pyfltr.config.Config, command: str) -> int:
    """config.command_names 内での位置を返す (未登録コマンドは末尾扱い)。"""
    if command in config.command_names:
        return config.command_names.index(command)
    return len(config.command_names)


def write_jsonl(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    exit_code: int,
    destination: pathlib.Path | None,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
) -> None:
    """JSONL を stdout もしくは指定ファイルに書き出す。

    destination が None のときは `sys.stdout` に書く。ファイル指定時は
    親ディレクトリを自動作成し、atomic write せず単純に上書きする
    (LLM 用途の使い捨てのため)。
    """
    lines = build_lines(results, config, exit_code=exit_code, commands=commands, files=files, warnings=warnings)
    if destination is None:
        for line in lines:
            sys.stdout.write(line)
            sys.stdout.write("\n")
        sys.stdout.flush()
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as f:
        for line in lines:
            f.write(line)
            f.write("\n")


def write_jsonl_header(commands: list[str], files: int) -> None:
    """header行をstdoutに書き出す（ストリーミングモード用）。

    パイプライン開始直後、diagnostic行より前に1回だけ呼ぶ。
    """
    with _write_lock:
        sys.stdout.write(_dump(_build_header_record(commands, files)))
        sys.stdout.write("\n")
        sys.stdout.flush()


def write_jsonl_streaming(
    result: pyfltr.command.CommandResult,
    config: pyfltr.config.Config,
) -> None:
    """1コマンド分のdiagnostic行+tool行をstdoutに即時書き出す。

    ``_write_lock``取得下で書き出し+flushするため、並列実行されるlinters/testers
    から呼ばれてもツール単位のグルーピングが崩れない。
    """
    lines = build_tool_lines(result, config)
    with _write_lock:
        for line in lines:
            sys.stdout.write(line)
            sys.stdout.write("\n")
        sys.stdout.flush()


def write_jsonl_footer(
    results: list[pyfltr.command.CommandResult],
    *,
    exit_code: int,
    warnings: list[dict[str, typing.Any]] | None = None,
) -> None:
    """warning行+summary行をstdoutに書き出す。

    ``results``は``_build_summary_record()``の集計に使用する。
    """
    with _write_lock:
        for warning in warnings or []:
            sys.stdout.write(_dump(_build_warning_record(warning)))
            sys.stdout.write("\n")
        sys.stdout.write(_dump(_build_summary_record(results, exit_code=exit_code)))
        sys.stdout.write("\n")
        sys.stdout.flush()


def _dump(record: dict[str, typing.Any]) -> str:
    """JSON 1 行にシリアライズする。ensure_ascii=False + 区切り最短化でトークン効率を稼ぐ。"""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _build_header_record(commands: list[str], files: int) -> dict[str, typing.Any]:
    """実行環境の基本情報を header レコード dict として返す。"""
    return {
        "kind": "header",
        "version": importlib.metadata.version("pyfltr"),
        "python": sys.version,
        "executable": sys.executable,
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "commands": commands,
        "files": files,
    }


def _build_warning_record(entry: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """警告 dict を warning レコード dict に変換する。"""
    return {
        "kind": "warning",
        "source": entry["source"],
        "msg": entry["message"],
    }


def _build_diagnostic_record(error: pyfltr.error_parser.ErrorLocation) -> dict[str, typing.Any]:
    """ErrorLocation を diagnostic レコード dict に変換する。None のフィールドは省略。"""
    record: dict[str, typing.Any] = {
        "kind": "diagnostic",
        "tool": error.command,
        "file": error.file,
        "line": error.line,
    }
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


def _build_tool_record(result: pyfltr.command.CommandResult, *, diagnostics: int) -> dict[str, typing.Any]:
    """CommandResult を tool レコード dict に変換する。

    `failed` かつ `diagnostics == 0` のときに限り、`CommandResult.output` の末尾を
    `_truncate_message()` でトリムして `message` フィールドを付与する。
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
    if result.status == "failed" and diagnostics == 0:
        message = _truncate_message(result.output)
        if message:
            record["message"] = message
    return record


def _build_summary_record(
    ordered_results: list[pyfltr.command.CommandResult],
    *,
    exit_code: int,
) -> dict[str, typing.Any]:
    """ordered_results から集計して summary レコード dict を作る。"""
    counts = {"succeeded": 0, "formatted": 0, "failed": 0, "skipped": 0}
    total_diagnostics = 0
    for result in ordered_results:
        counts[result.status] = counts.get(result.status, 0) + 1
        total_diagnostics += len(result.errors)
    return {
        "kind": "summary",
        "total": len(ordered_results),
        "succeeded": counts["succeeded"],
        "formatted": counts["formatted"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "diagnostics": total_diagnostics,
        "exit": exit_code,
    }


def _truncate_message(output: str) -> str:
    r"""生出力を末尾 30 行かつ 2000 文字にトリムする。トリム時は先頭に `"... (truncated)\n"` を付与する。

    空文字は空文字をそのまま返す (呼び出し側で message キーごと省略する)。
    """
    if not output:
        return ""
    lines = output.splitlines()
    truncated = False
    if len(lines) > _MESSAGE_MAX_LINES:
        lines = lines[-_MESSAGE_MAX_LINES:]
        truncated = True
    body = "\n".join(lines)
    if len(body) > _MESSAGE_MAX_CHARS:
        body = body[-_MESSAGE_MAX_CHARS:]
        truncated = True
    if truncated:
        return _TRUNCATED_PREFIX + body
    return body
