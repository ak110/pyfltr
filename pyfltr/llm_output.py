"""LLM 向け JSON Lines 出力。

`--output-format=jsonl` で呼ばれ、CommandResult 群を LLM / エージェントが
読みやすいフラットな JSON Lines 形式 (diag / tool / summary の 3 種別) に
変換して書き出す。
"""

import json
import pathlib
import sys
import typing

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser

# failed かつ diags=0 のときに tool.message として載せる生出力のトリム上限。
# 末尾 30 行を取り出し、さらに末尾 2000 文字に切り詰める。
_MESSAGE_MAX_LINES = 30
_MESSAGE_MAX_CHARS = 2000
_TRUNCATED_PREFIX = "... (truncated)\n"


def build_lines(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    exit_code: int,
) -> list[str]:
    """CommandResult 群から JSONL 各行を生成する。

    出力順:
        1. 全診断を (file, line, col, command 順) で昇順ソートした diag 行
        2. config.command_names の定義順に並べた tool 行
        3. summary 行 1 行

    results は順序を問わない。内部で `config.command_names` 順にソートする。
    """
    ordered = sorted(results, key=lambda r: _command_index(config, r.command))

    lines: list[str] = []

    all_errors: list[pyfltr.error_parser.ErrorLocation] = []
    for result in ordered:
        all_errors.extend(result.errors)
    sorted_errors = pyfltr.error_parser.sort_errors(all_errors, config.command_names)
    for error in sorted_errors:
        lines.append(_dump(_build_diag_record(error)))

    diag_counts: dict[str, int] = {}
    for error in all_errors:
        diag_counts[error.command] = diag_counts.get(error.command, 0) + 1

    for result in ordered:
        diags = diag_counts.get(result.command, 0)
        lines.append(_dump(_build_tool_record(result, diags=diags)))

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
) -> None:
    """JSONL を stdout もしくは指定ファイルに書き出す。

    destination が None のときは `sys.stdout` に書く。ファイル指定時は
    親ディレクトリを自動作成し、atomic write せず単純に上書きする
    (LLM 用途の使い捨てのため)。
    """
    lines = build_lines(results, config, exit_code=exit_code)
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


def _dump(record: dict[str, typing.Any]) -> str:
    """JSON 1 行にシリアライズする。ensure_ascii=False + 区切り最短化でトークン効率を稼ぐ。"""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _build_diag_record(error: pyfltr.error_parser.ErrorLocation) -> dict[str, typing.Any]:
    """ErrorLocation を diag レコード dict に変換する。None のフィールドは省略。"""
    record: dict[str, typing.Any] = {
        "kind": "diag",
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


def _build_tool_record(result: pyfltr.command.CommandResult, *, diags: int) -> dict[str, typing.Any]:
    """CommandResult を tool レコード dict に変換する。

    `failed` かつ `diags == 0` のときに限り、`CommandResult.output` の末尾を
    `_truncate_message()` でトリムして `message` フィールドを付与する。
    """
    record: dict[str, typing.Any] = {
        "kind": "tool",
        "tool": result.command,
        "type": result.command_type,
        "status": result.status,
        "files": result.files,
        "elapsed": round(result.elapsed, 2),
        "diags": diags,
    }
    if result.returncode is not None:
        record["rc"] = result.returncode
    if result.status == "failed" and diags == 0:
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
    total_diags = 0
    for result in ordered_results:
        counts[result.status] = counts.get(result.status, 0) + 1
        total_diags += len(result.errors)
    return {
        "kind": "summary",
        "total": len(ordered_results),
        "succeeded": counts["succeeded"],
        "formatted": counts["formatted"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "diags": total_diags,
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
