"""grep / replaceサブコマンドのJSONLレコード生成。

`pyfltr.output.jsonl.emit_record`の公開ヘルパーを経由して書き込むことで、
既存のJSONL出力経路（heartbeat連動・最終出力時刻記録）と挙動を揃える。
新規`kind`を追加する場合も本モジュールに追加し、`emit_record`経由で出力する。
"""

from __future__ import annotations

import json
import pathlib
import typing

import pyfltr.output.jsonl
import pyfltr.paths
from pyfltr.grep_.types import MatchRecord, ReplaceRecord


def emit_warning(entry: dict[str, typing.Any]) -> None:
    """`pyfltr.warnings_`蓄積由来の警告dictを`kind:"warning"`レコードとして出力する。

    フィールド順は`pyfltr/output/jsonl.py`の`_build_warning_record`と揃え、
    pipeline既存のwarning出力と挙動を一致させる。
    `entry`は`source`・`message`を必須、`hint`を任意とする。
    """
    record: dict[str, typing.Any] = {
        "kind": "warning",
        "source": entry["source"],
        "msg": entry["message"],
    }
    hint = entry.get("hint")
    if hint is not None:
        record["hint"] = hint
    _emit(record)


def emit_grep_header(
    *,
    pattern: str,
    files: int,
    run_id: str | None = None,
    format_source: str | None = None,
) -> None:
    """grep開始時のheaderレコードを1件出力する。

    `pattern`は結合済みの正規表現原文（複数パターンは`|`連結後の文字列）を指定する。
    """
    record: dict[str, typing.Any] = {
        "kind": "header",
        "subcommand": "grep",
        "pattern": pattern,
        "files": files,
    }
    if run_id is not None:
        record["run_id"] = run_id
    if format_source is not None:
        record["format_source"] = format_source
    _emit(record)


def emit_match(record: MatchRecord) -> None:
    """grepの1マッチをJSONLレコードとして出力する。

    `before`・`after`は`-B`・`-A`コンテキストで取得した行群を指定する。
    どちらも空の場合はキーを省略してトークン消費を抑える。
    """
    payload: dict[str, typing.Any] = {
        "kind": "match",
        "file": pyfltr.paths.normalize_separators(str(record.file)),
        "line": record.line,
        "col": record.col,
        "match_text": record.match_text,
        "line_text": record.line_text,
    }
    if record.end_col is not None:
        payload["end_col"] = record.end_col
    if record.before_lines:
        payload["before"] = list(record.before_lines)
    if record.after_lines:
        payload["after"] = list(record.after_lines)
    _emit(payload)


def emit_grep_summary(
    *,
    total_matches: int,
    files_scanned: int,
    exit_code: int,
    guidance: list[str] | None = None,
) -> None:
    """grep完了時のsummaryレコードを1件出力する。

    `guidance`は英語のヒント文字列リスト（replace起動コマンド案内など）を指定する。
    指摘・該当事項がある場合のみ呼び出し側で組み立てる方針とし、空リストは省略する。
    """
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "subcommand": "grep",
        "exit": exit_code,
        "total_matches": total_matches,
        "files_scanned": files_scanned,
    }
    if guidance:
        record["guidance"] = list(guidance)
    _emit(record)


def emit_replace_header(
    *,
    pattern: str,
    replacement: str,
    files: int,
    replace_id: str | None,
    dry_run: bool,
    format_source: str | None = None,
) -> None:
    """replace開始時のheaderレコードを1件出力する。

    `replace_id`はdry-run時には`None`を指定する。
    """
    record: dict[str, typing.Any] = {
        "kind": "header",
        "subcommand": "replace",
        "pattern": pattern,
        "replacement": replacement,
        "files": files,
        "dry_run": dry_run,
    }
    if replace_id is not None:
        record["replace_id"] = replace_id
    if format_source is not None:
        record["format_source"] = format_source
    _emit(record)


def emit_file_change(
    *,
    file: pathlib.Path,
    count: int,
    before_hash: str | None,
    after_hash: str | None,
    dry_run: bool,
    records: list[ReplaceRecord] | None = None,
    show_changes: bool = False,
) -> None:
    """1ファイル分の置換結果をJSONLレコードとして出力する。

    `show_changes=True`のときは`changes`配列に各置換箇所の`before_line`・`after_line`を
    含める。トークン消費が増えるため、`--show-changes`明示時のみ有効化する。
    """
    payload: dict[str, typing.Any] = {
        "kind": "file_change",
        "file": pyfltr.paths.normalize_separators(str(file)),
        "count": count,
        "dry_run": dry_run,
    }
    if before_hash is not None:
        payload["before_hash"] = before_hash
    if after_hash is not None:
        payload["after_hash"] = after_hash
    if show_changes and records:
        payload["changes"] = [
            {
                "line": r.line,
                "col": r.col,
                "before_line": r.before_line,
                "after_line": r.after_line,
            }
            for r in records
        ]
    _emit(payload)


def emit_replace_summary(
    *,
    files_changed: int,
    total_replacements: int,
    exit_code: int,
    replace_id: str | None,
    dry_run: bool,
    guidance: list[str] | None = None,
) -> None:
    """replace完了時のsummaryレコードを1件出力する。"""
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "subcommand": "replace",
        "exit": exit_code,
        "files_changed": files_changed,
        "total_replacements": total_replacements,
        "dry_run": dry_run,
    }
    if replace_id is not None:
        record["replace_id"] = replace_id
    if guidance:
        record["guidance"] = list(guidance)
    _emit(record)


def emit_file_with_matches(file: pathlib.Path, count: int) -> None:
    """`--files-with-matches`相当のレコードを1件出力する。"""
    _emit(
        {
            "kind": "file_with_matches",
            "file": pyfltr.paths.normalize_separators(str(file)),
            "count": count,
        }
    )


def emit_file_without_match(file: pathlib.Path) -> None:
    """`--files-without-match`相当のレコードを1件出力する。"""
    _emit({"kind": "file_without_match", "file": pyfltr.paths.normalize_separators(str(file))})


def emit_file_count(file: pathlib.Path, count: int) -> None:
    """`--count` / `--count-matches`相当のレコードを1件出力する。"""
    _emit(
        {
            "kind": "file_count",
            "file": pyfltr.paths.normalize_separators(str(file)),
            "count": count,
        }
    )


def emit_replace_history(meta: dict[str, typing.Any]) -> None:
    """`--list-history` / `--show-history` 用の履歴レコードを1件出力する。

    `meta`は`ReplaceHistoryStore.load_replace`相当の辞書（`replace_id` / `saved_at` /
    `command` / `files`等を含む）を想定する。
    """
    payload = {"kind": "replace_history", **meta}
    _emit(payload)


def emit_replace_undo_summary(
    *,
    replace_id: str,
    restored: list[pathlib.Path],
    skipped: list[pathlib.Path],
    exit_code: int,
) -> None:
    """undo完了時のsummaryレコードを1件出力する。

    `skipped`が非空のときは利用者に手動編集の存在を通知し、`--force`での強制復元を促す
    ガイダンスを呼び出し側で添える運用とする。
    """
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "subcommand": "replace_undo",
        "exit": exit_code,
        "replace_id": replace_id,
        "restored": [pyfltr.paths.normalize_separators(str(p)) for p in restored],
        "skipped": [pyfltr.paths.normalize_separators(str(p)) for p in skipped],
    }
    _emit(record)


def _emit(record: dict[str, typing.Any]) -> None:
    """構造化出力を1行JSONとして書き込む。

    `pyfltr.output.jsonl.emit_record`の公開ヘルパー経由で書き込み、
    SSOTのwrite経路（`_write_lock`保護・最終出力時刻更新）を共有する。
    grep / replaceの出力は逐次的でグルーピング不要のため、1レコード単位の発行で十分。
    """
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    pyfltr.output.jsonl.emit_record(line)
