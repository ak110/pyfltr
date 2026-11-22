import importlib.metadata
import json
import logging
import os
import sys
import typing

import pyfltr.command.completion
import pyfltr.command.core_
import pyfltr.command.mise
import pyfltr.command.runner
import pyfltr.config.model
import pyfltr.diagnostics
import pyfltr.output.guidance
import pyfltr.paths

logger = logging.getLogger(__name__)

_TRUNCATED_MARKER = "\n... (truncated)\n"
"""ハイブリッド切り詰めで先頭ブロックと末尾ブロックの間に挿入する区切りマーカー。

旧仕様では先頭の`... (truncated)\n`のみだったが、エラーが冒頭に出力されるツール
（editorconfig-checkerなど）で重要情報が末尾の省略範囲に移動する問題を避けるため、
中央にマーカーを置く形式へ変更している。
"""


_DEFAULT_HEAD_RATIO = 0.2
"""ハイブリッド切り詰め時に先頭側へ確保する割合。

合計上限を`head : tail = 1 : 4`で配分する根拠は次の通り。
冒頭にエラー要約を出力するツール（editorconfig-checker等）を捕捉するのに1KBあれば実例上十分で、
末尾優先の従来挙動で捕捉できていた多行スタックトレース系（mypy・pytest等）を温存するために
末尾には残り4KBを割り当てる。
"""


def aggregate_diagnostics(
    errors: typing.Iterable[pyfltr.diagnostics.ErrorLocation],
) -> tuple[list[dict[str, typing.Any]], dict[str, str], dict[str, str]]:
    groups: dict[tuple[str, str], list[pyfltr.diagnostics.ErrorLocation]] = {}
    group_order: list[tuple[str, str]] = []
    hint_urls: dict[str, str] = {}
    hints: dict[str, str] = {}
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
                logger.debug(
                    "aggregate_diagnostics: rule %r に複数のrule_urlが存在する。先勝ち採用: %s (後続 %s を無視)",
                    error.rule,
                    existing,
                    error.rule_url,
                )
        if error.rule is not None and error.hint is not None:
            existing_hint = hints.get(error.rule)
            if existing_hint is None:
                hints[error.rule] = error.hint
            elif existing_hint != error.hint:
                logger.debug(
                    "aggregate_diagnostics: rule %r に複数のhintが存在する。先勝ち採用: %s (後続 %s を無視)",
                    error.rule,
                    existing_hint,
                    error.hint,
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
                "command": command,
                "file": file,
                "messages": [build_message_dict(e) for e in sorted_messages],
            }
        )
    return records, hint_urls, hints


def collect_mise_active_tools_for_header(
    commands: list[str], config: pyfltr.config.model.Config
) -> dict[str, typing.Any] | None:
    if not any(pyfltr.command.runner.get_mise_active_tool_key(cmd) is not None for cmd in commands):
        return None
    result = pyfltr.command.mise.get_mise_active_tools(config)
    info: dict[str, typing.Any] = {"status": result.status}
    if result.detail is not None:
        info["detail"] = result.detail
    if result.status == "ok":
        info["active_keys"] = sorted(result.tools.keys())
    return info


def dump_record(record: dict[str, typing.Any]) -> str:
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def build_header_record(
    commands: list[str],
    files: int,
    *,
    run_id: str | None = None,
    mise_active_tools: dict[str, typing.Any] | None = None,
    format_source: str | None = None,
    quiet: bool = False,
) -> dict[str, typing.Any]:
    if quiet:
        condensed: dict[str, typing.Any] = {"kind": "header", "commands": commands, "files": files}
        if run_id is not None:
            condensed["run_id"] = run_id
        return condensed
    record: dict[str, typing.Any] = {
        "kind": "header",
        "version": importlib.metadata.version("pyfltr"),
        "python": sys.version,
        "executable": sys.executable,
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "files": files,
        "commands": commands,
        "uv": {
            "lock": pyfltr.command.runner.cwd_has_uv_lock(),
            "available": pyfltr.command.runner.ensure_uv_available(),
            "x_available": pyfltr.command.runner.ensure_uvx_available(),
        },
    }
    if run_id is not None:
        record["run_id"] = run_id
    if mise_active_tools is not None:
        record["mise_active_tools"] = mise_active_tools
    if format_source is not None:
        record["format_source"] = format_source
    return record


def build_warning_record(entry: dict[str, typing.Any]) -> dict[str, typing.Any]:
    record: dict[str, typing.Any] = {
        "kind": "warning",
        "source": entry["source"],
        "msg": entry["message"],
        "hint": entry["hint"],
    }
    return record


def build_message_dict(error: pyfltr.diagnostics.ErrorLocation) -> dict[str, typing.Any]:
    record: dict[str, typing.Any] = {"line": error.line}
    if error.col is not None:
        record["col"] = error.col
    if error.end_line is not None:
        record["end_line"] = error.end_line
    if error.end_col is not None:
        record["end_col"] = error.end_col
    if error.rule is not None:
        record["rule"] = error.rule
    if error.severity is not None:
        record["severity"] = error.severity
    if error.fix is not None:
        record["fix"] = error.fix
    record["msg"] = error.message
    return record


def build_truncated_meta(
    result: pyfltr.command.core_.CommandResult,
    *,
    diagnostics: int,
    diagnostic_total: int | None,
    config: pyfltr.config.model.Config | None,
) -> tuple[dict[str, typing.Any], str | None]:
    truncated: dict[str, typing.Any] = {}
    message: str | None = None
    archive_command_dir = pyfltr.paths.sanitize_command_name(result.command)
    if diagnostic_total is not None and diagnostic_total > diagnostics:
        truncated["diagnostics_total"] = diagnostic_total
        truncated["archive"] = f"tools/{archive_command_dir}/diagnostics.jsonl"

    if result.failed and (diagnostics == 0 or result.command_type == "tester"):
        message_max_lines, message_max_chars = _resolve_message_limits(config)
        msg, msg_truncated, head_chars, tail_chars = _truncate_message(
            result.output,
            max_lines=message_max_lines,
            max_chars=message_max_chars,
            archived=result.archived,
        )
        if msg:
            message = msg
        if msg_truncated:
            truncated["lines"] = len(result.output.splitlines())
            truncated["chars"] = len(result.output)
            truncated["head_chars"] = head_chars
            truncated["tail_chars"] = tail_chars
            truncated.setdefault("archive", f"tools/{archive_command_dir}/output.log")

    return truncated, message


def build_hints_dict(
    result: pyfltr.command.core_.CommandResult,
    *,
    diagnostics: int,
    hints: dict[str, str] | None,
    config: pyfltr.config.model.Config | None,
) -> dict[str, str]:
    merged: dict[str, str] = dict(hints) if hints else {}
    if result.command == "textlint" and diagnostics > 0:
        merged["messages[].col"] = (
            "textlint reports col and end_col as cumulative offsets from the text-node start, not in-line offsets"
        )
    if config is not None and diagnostics > 0:
        user_hints: list[str] = list(pyfltr.config.model.command_setting(config.values, result.command, "hints", []))
        for index, hint_text in enumerate(user_hints):
            merged[f"user.{index}"] = hint_text
    if result.formatted:
        merged["status.formatted"] = (
            "formatter rewrote files; rerun is not required because the rewrite itself counts as success"
        )
    if result.timeout_exceeded:
        merged["status.timeout"] = (
            "subprocess was killed because it exceeded the configured timeout;"
            " inspect command.message for the captured tail and consider adjusting `command-timeout`"
            " or the per-tool `{command}-timeout`"
        )
    return merged


def build_command_record(
    result: pyfltr.command.core_.CommandResult,
    *,
    diagnostics: int,
    diagnostic_total: int | None = None,
    config: pyfltr.config.model.Config | None = None,
    hint_urls: dict[str, str] | None = None,
    hints: dict[str, str] | None = None,
) -> dict[str, typing.Any]:
    record: dict[str, typing.Any] = {
        "kind": "command",
        "command": result.command,
        "type": result.command_type,
        "status": result.status,
    }
    if result.runner_fallback is not None:
        record["effective_runner"] = result.effective_runner
        record["runner_source"] = result.runner_source
        record["runner_fallback"] = result.runner_fallback
    record["files"] = result.files
    record["diagnostics"] = diagnostics
    elapsed_key = "cached_elapsed" if result.cached else "elapsed"
    record[elapsed_key] = round(result.elapsed, 2)
    if result.returncode is not None:
        record["rc"] = result.returncode
    if result.retry_count:
        record["retry_count"] = result.retry_count
    if result.slow_tests:
        record["slow_tests"] = [test.to_dict() for test in result.slow_tests]

    truncated, message = build_truncated_meta(result, diagnostics=diagnostics, diagnostic_total=diagnostic_total, config=config)
    if message is not None:
        record["message"] = message
    if truncated:
        record["truncated"] = truncated
    if result.retry_command is not None:
        record["retry_command"] = result.retry_command
    if result.cached:
        record["cached"] = True
        if result.cached_from is not None:
            record["cached_from"] = result.cached_from
    if hint_urls:
        record["hint_urls"] = dict(hint_urls)
    merged_hints = build_hints_dict(result, diagnostics=diagnostics, hints=hints, config=config)
    if merged_hints:
        record["hints"] = merged_hints
    return record


def _resolve_message_limits(config: pyfltr.config.model.Config | None) -> tuple[int, int]:
    if config is None:
        return 30, 2000
    max_lines = int(config.values.get("jsonl-message-max-lines", 30) or 0)
    max_chars = int(config.values.get("jsonl-message-max-chars", 2000) or 0)
    return max_lines, max_chars


def build_summary_record(
    ordered_results: list[pyfltr.command.core_.CommandResult],
    *,
    exit_code: int,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
    subcommand: str | None = None,
    fully_excluded_files: list[str] | None = None,
    missing_targets: list[str] | None = None,
    warning_count: int = 0,
    completion: pyfltr.command.completion.RunCompletion | None = None,
) -> dict[str, typing.Any]:
    """ordered_resultsから集計してsummaryレコードdictを生成する。

    `subcommand`は`summary.guidance`の再実行例に埋め込む実行系サブコマンド名。

    集計カウンタはコマンド単位の集計であることを示す`commands_summary`配下にまとめ、
    その下で「対応不要」「対応要」の2グループへネストする。
    `commands_summary.no_issues`配下に`succeeded`/`formatted`/`skipped`を、
    `commands_summary.needs_action`配下に`failed`/`warning`を並べる。
    `formatted`は本リポジトリの運用上「再実行や追加対応は原則不要」と整理し
    `no_issues`側に分類する。
    `resolution_failed`は0件のときキー自体を省略する（通常プロジェクトでは常に0のため）。
    `failed`/`warning`は0件でも常時出力する（0件であることがエラー無し / 警告無し判定に直結するため）。
    `commands_summary`の兄弟である`applied_fixes`/`fully_excluded_files`/
    `missing_targets`/`guidance`はカウント集計ではないため移動しない。
    `fully_excluded_files`が非空のとき、直接指定されたがexcludeパターン・.gitignore
    によって全除外されたファイル一覧を`fully_excluded_files`キーに埋め込む。
    `missing_targets`が非空のとき、直接指定されたが存在しないファイル一覧を
    `missing_targets`キーに埋め込む（exclude/.gitignore全除外と原因を区別するため
    別フィールドで併存させる）。
    `warning_count`が1以上のとき、対象の実行で出力した`kind:"warning"`レコードの件数を
    `warnings`キーに埋め込む。同キーは実行時警告の件数であり、
    `commands_summary.needs_action.warning`（`{command}-severity`によるコマンド結果の
    格下げ件数）とは集計対象が異なる。呼び出し側はwarningレコードの生成元と同一の引数から
    件数を導出して渡し、レコード件数とsummaryの値を構造的に一致させる。
    `completion`が渡された場合は`completion`・`files_reached`・`completed_commands`・
    `incomplete_commands`を空一覧も含めて常に埋め込む。値は`pyfltr.command.completion`が導出し、
    MCP `run`の応答と同じ値になる。`run_pipeline`は全経路で渡すため、
    省略されるのはパイプライン外からの直接呼び出しだけとなる。

    フィールド順序ルール:

    - 必須キー順は`kind` → `exit` → `commands_summary` → `diagnostics`。
      結論を上位に置きLLMが読み下しやすい流れに揃える。
    - 完了判定の4キーは`exit`の直後に`completion` → `files_reached` → `completed_commands` →
      `incomplete_commands`の順で置く。終了コードと並ぶ結論として最初に読めるようにするため。
    - 条件付きキー順は`warnings` → `guidance` → `applied_fixes` → `fully_excluded_files` →
      `missing_targets`。
      警告件数 → 解釈支援（次に取るべき行動）→ 自動適用結果 → 除外検知 → 不在検知の流れに揃える。
    - コマンド総数`total`は`commands_summary`配下の末尾（`no_issues` / `needs_action`の後）に置く。
      カテゴリ集計を読んでから総数を確認できる順序とするため。
    - 指摘総件数`diagnostics`はコマンド単位の集計ではないため`commands_summary`には含めない。
      集計の意味分類が混ざらないようにするため。
    - `applied_fixes`はfixステージ・formatterステージで実際に内容変化したファイルパスを
      全コマンドにわたってユニオンしソートした一覧。変化なしの場合は省略する。

    JSONLスキーマの変更は破壊的変更扱いしない（LLMが読みやすいよう継続的に改善する）。
    新規キーの追加・既存キー名の調整時は本docstringと利用者向けドキュメント
    （`docs/guide/usage.md`）を併せて更新する。
    """
    counts = {"succeeded": 0, "formatted": 0, "failed": 0, "warning": 0, "resolution_failed": 0, "skipped": 0}
    total_diagnostics = 0
    fixed_files_union: set[str] = set()
    for result in ordered_results:
        counts[result.status] = counts.get(result.status, 0) + 1
        total_diagnostics += len(result.errors)
        if result.fixed_files:
            fixed_files_union.update(result.fixed_files)
    needs_action: dict[str, typing.Any] = {"failed": counts["failed"], "warning": counts["warning"]}
    if counts["resolution_failed"] > 0:
        needs_action["resolution_failed"] = counts["resolution_failed"]
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "exit": exit_code,
    }
    if completion is not None:
        record["completion"] = completion.completion
        record["files_reached"] = completion.files_reached
        record["completed_commands"] = list(completion.completed_commands)
        record["incomplete_commands"] = list(completion.incomplete_commands)
    record["commands_summary"] = {
        "no_issues": {
            "succeeded": counts["succeeded"],
            "formatted": counts["formatted"],
            "skipped": counts["skipped"],
        },
        "needs_action": needs_action,
        "total": len(ordered_results),
    }
    record["diagnostics"] = total_diagnostics
    if warning_count > 0:
        record["warnings"] = warning_count
    guidance = pyfltr.output.guidance.build_summary_guidance(
        failure_present=counts["failed"] + counts["resolution_failed"] > 0,
        resolution_failed_present=counts["resolution_failed"] > 0,
        applied_fixes_present=bool(fixed_files_union),
        run_id=run_id,
        launcher_prefix=launcher_prefix,
        subcommand=subcommand,
    )
    if guidance:
        record["guidance"] = guidance
    if fixed_files_union:
        record["applied_fixes"] = sorted(fixed_files_union)
    if fully_excluded_files:
        record["fully_excluded_files"] = list(fully_excluded_files)
    if missing_targets:
        record["missing_targets"] = list(missing_targets)
    return record


def _truncate_message(
    output: str,
    *,
    max_lines: int,
    max_chars: int,
    archived: bool,
) -> tuple[str, bool, int, int]:
    if not output:
        return "", False, 0, 0
    if not archived:
        return output, False, 0, 0

    line_count = output.count("\n") + (0 if output.endswith("\n") else 1)
    needs_truncate_chars = 0 < max_chars < len(output)
    needs_truncate_lines = 0 < max_lines < line_count
    if not needs_truncate_chars and not needs_truncate_lines:
        return output, False, 0, 0

    char_budget = max_chars if max_chars > 0 else len(output)
    head_size = max(1, int(char_budget * _DEFAULT_HEAD_RATIO))
    tail_size = char_budget - head_size
    overhead = len(_TRUNCATED_MARKER)
    if tail_size > overhead:
        tail_size -= overhead

    head_block = output[:head_size]
    tail_block = output[-tail_size:] if tail_size < len(output) else output

    if max_lines > 0:
        tail_lines = tail_block.splitlines()
        if len(tail_lines) > max_lines:
            tail_block = "\n".join(tail_lines[-max_lines:])

    return head_block + _TRUNCATED_MARKER + tail_block, True, len(head_block), len(tail_block)
