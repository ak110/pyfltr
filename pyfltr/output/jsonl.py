"""LLM向けJSON Lines出力。

`--output-format=jsonl`で呼ばれ、CommandResult群をLLM / エージェントが
読みやすいフラットなJSON Lines形式（header / diagnostic / command / warning / summaryの5種別）に
変換して書き出す。

`diagnostic`は`(command, file)`単位で集約し、個別指摘は`messages[]`に格納する。
ルールURLはcommand単位の`hint_urls`辞書へ、ルール別の短い修正ヒントは`hints`辞書へそれぞれ集約し、
LLM入力時のトークン浪費を抑える。
"""

import importlib.metadata
import json
import logging
import os
import shlex
import sys
import threading
import typing

import pyfltr.cli.output_format
import pyfltr.command
import pyfltr.config.config
import pyfltr.error_parser
import pyfltr.paths

logger = logging.getLogger(__name__)

# ストリーミング書き出し時に複数行（diagnostic行+tool行）をアトミックに出力するためのロック。
# 並列実行されるlinters/testersから同時にコールバックが呼ばれる可能性がある。
# 出力先は`pyfltr.cli.output_format.structured_logger`のhandlerに委ねるが、ログ1件 = 1行の
# 粒度では複数行のグルーピングを保証できないためモジュール側でロックする。
_write_lock = threading.Lock()

_TRUNCATED_MARKER = "\n... (truncated)\n"
"""ハイブリッド切り詰めで先頭ブロックと末尾ブロックの間に挿入する区切りマーカー。

旧仕様では先頭の`... (truncated)\n`のみだったが、エラーが冒頭に出力されるツール
（editorconfig-checkerなど）で重要情報が末尾切り捨て側に落ちる問題を避けるため、
中央にマーカーを置く形式へ変更している。
"""

_DEFAULT_HEAD_RATIO = 0.2
"""ハイブリッド切り詰め時に先頭側へ確保する割合。

合計上限を`head : tail = 1 : 4`で配分する根拠は次の通り。
冒頭にエラー要約を出すツール（editorconfig-checker等）を救うのに1KBあれば実例上十分で、
末尾優先の従来挙動で救えていた多行スタックトレース系（mypy・pytest等）を温存するために
末尾には残り4KBを割り当てる。
"""


def build_command_lines(
    result: pyfltr.command.CommandResult,
    config: pyfltr.config.config.Config,
) -> list[str]:
    """1コマンド分のdiagnostic行+command行をJSONL文字列のリストとして生成する。

    diagnostic行は`(command, file)`単位で集約され、個別指摘は`messages[]`に並ぶ。
    個別指摘の合計件数が`jsonl-diagnostic-limit`を超える場合は
    `ErrorLocation`列を先頭N件で切ってから集約し、
    commandレコードに`truncated.diagnostics_total`を添付する。
    切り詰めは`result.archived`がTrueのときのみ適用し、Falseの場合は全件出力する
    （アーカイブから復元不能な情報欠落を防ぐため）。

    判定単位はステージごとの`CommandResult`単位とする。fixステージと
    通常ステージは同じ`command`名で別`CommandResult`として渡されるため、
    片方のアーカイブ書き込み失敗が他方の切り詰め可否に影響しない。
    """
    sorted_errors = pyfltr.error_parser.sort_errors(result.errors, config.command_names)
    diagnostic_total = len(sorted_errors)
    diagnostic_limit = int(config.values.get("jsonl-diagnostic-limit", 0) or 0)

    diagnostics_truncated = False
    if 0 < diagnostic_limit < diagnostic_total and result.archived:
        sorted_errors = sorted_errors[:diagnostic_limit]
        diagnostics_truncated = True

    diagnostic_records, hint_urls, hints = aggregate_diagnostics(sorted_errors)

    lines: list[str] = []
    for record in diagnostic_records:
        lines.append(_dump(record))
    lines.append(
        _dump(
            _build_command_record(
                result,
                diagnostics=len(sorted_errors),
                diagnostic_total=diagnostic_total if diagnostics_truncated else None,
                config=config,
                hint_urls=hint_urls,
                hints=hints,
            )
        )
    )
    return lines


def aggregate_diagnostics(
    errors: typing.Iterable[pyfltr.error_parser.ErrorLocation],
) -> tuple[list[dict[str, typing.Any]], dict[str, str], dict[str, str]]:
    """`ErrorLocation`列を`(command, file)`単位の集約dictへ変換する。

    戻り値は`(diagnostic_records, hint_urls, hints)`の3要素タプル。

    - `diagnostic_records`: `diagnostic`レコードのリスト。各要素は
      `{"kind": "diagnostic", "command": ..., "file": ..., "messages": [...]}`。
      `messages`は`(line, col or 0, rule or "")`昇順で並ぶ。
      ruleキーを含めるのは、同一`(file, line, col)`に複数ルールの指摘が
      重なる場合でも安定した順序を保証するため（ruleなし要素は空文字列扱いで
      先頭側にまとまる）。
    - `hint_urls`: rule→URL辞書（`command.hint_urls`キーで埋め込む用）。URLが生成できたruleのみ含む。
    - `hints`: rule→短い修正ヒント辞書（`command.hints`キーで埋め込む用）。
      `ErrorLocation.hint`が非Noneのruleのみ含む。

    同一ruleに異なる値（URL / hint）が紛れた場合は先に出現した値を採用してwarningログを残す。
    先勝ち採用にしているのは、ツール単位の辞書としては1ルール1値に束ねる方がLLM入力時の混乱が
    少ないため。逸脱はwarningログで気付ける余地を残す。
    集約のキー順は入力順（`sort_errors()`済み）を尊重する。
    """
    groups: dict[tuple[str, str], list[pyfltr.error_parser.ErrorLocation]] = {}
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
                logger.warning(
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
                logger.warning(
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
                "messages": [_build_message_dict(e) for e in sorted_messages],
            }
        )
    return records, hint_urls, hints


def build_lines(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.config.Config,
    *,
    exit_code: int,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
    fully_excluded_files: list[str] | None = None,
    format_source: str | None = None,
) -> list[str]:
    """CommandResult群からJSONL各行を生成する。

    出力順:
        1. `commands`と`files`が指定されていればkind="header"行
        2. `warnings`が非空ならkind="warning"行
        3. コマンド単位でdiagnostic行+command行（`config.command_names`の定義順）
        4. summary行1行

    resultsは順序を問わない。内部で`config.command_names`順にソートする。
    `warnings`は`pyfltr.warnings_.collected_warnings()`の返り値を想定する。
    `run_id`が指定されていればheaderレコードに埋め込む。
    `launcher_prefix`が指定されていれば`summary.guidance`内の起動コマンド表記に反映する。
    """
    ordered = sorted(results, key=lambda r: _command_index(config, r.command))

    lines: list[str] = []

    if commands is not None and files is not None:
        lines.append(
            _dump(
                _build_header_record(
                    commands,
                    files,
                    run_id=run_id,
                    mise_active_tools=collect_mise_active_tools_for_header(commands, config),
                    format_source=format_source,
                )
            )
        )

    for warning in warnings or []:
        lines.append(_dump(_build_warning_record(warning)))

    for result in ordered:
        lines.extend(build_command_lines(result, config))

    lines.append(
        _dump(
            _build_summary_record(
                ordered,
                exit_code=exit_code,
                run_id=run_id,
                launcher_prefix=launcher_prefix,
                fully_excluded_files=fully_excluded_files,
            )
        )
    )
    return lines


def _command_index(config: pyfltr.config.config.Config, command: str) -> int:
    """config.command_names 内での位置を返す（未登録コマンドは末尾扱い）。"""
    if command in config.command_names:
        return config.command_names.index(command)
    return len(config.command_names)


def write_jsonl_header(
    commands: list[str],
    files: int,
    *,
    run_id: str | None = None,
    config: pyfltr.config.config.Config | None = None,
    format_source: str | None = None,
) -> None:
    """header行を構造化出力loggerに書き出す（ストリーミングモード用）。

    パイプライン開始直後、diagnostic行より前に1回だけ呼ぶ。`run_id`が指定されていれば
    headerレコードに含める（アーカイブ参照時の識別キー）。
    出力先は`pyfltr.cli.output_format.configure_structured_output()`が設定したhandlerに従う
    （stdoutもしくは`--output-file`のFileHandler）。
    `config`が渡された場合、mise経路を使うrunに限り
    `mise_active_tools`フィールドへ取得状況を露出する。
    `format_source`が渡された場合、`format_source`フィールドへ解決経路ラベルを露出する。
    """
    mise_active_tools = collect_mise_active_tools_for_header(commands, config) if config is not None else None
    with _write_lock:
        pyfltr.cli.output_format.structured_logger.info(
            _dump(
                _build_header_record(
                    commands,
                    files,
                    run_id=run_id,
                    mise_active_tools=mise_active_tools,
                    format_source=format_source,
                )
            )
        )


def collect_mise_active_tools_for_header(
    commands: list[str], config: pyfltr.config.config.Config
) -> dict[str, typing.Any] | None:
    """header露出用のmise active tools取得状況dictを組み立てる。

    `commands`にmiseバックエンド登録済みのものが1つも含まれない場合は`None`を返し、
    Python専用runのheaderへ無関係な`mise-not-found`を出さないようにする。
    含まれる場合は`_get_mise_active_tools(config)`を副作用OFFで引き、
    `{"status": ..., "detail": ..., "active_keys": [...]}`形式に整える。
    """
    if not any(pyfltr.command.get_mise_active_tool_key(cmd) is not None for cmd in commands):
        return None
    result = pyfltr.command._get_mise_active_tools(config)  # pylint: disable=protected-access
    info: dict[str, typing.Any] = {"status": result.status}
    if result.detail is not None:
        info["detail"] = result.detail
    if result.status == "ok":
        info["active_keys"] = sorted(result.tools.keys())
    return info


def write_jsonl_streaming(
    result: pyfltr.command.CommandResult,
    config: pyfltr.config.config.Config,
) -> None:
    """1コマンド分のdiagnostic行+command行を構造化出力loggerに即時書き出す。

    `_write_lock`取得下で複数行を連続書き出しすることで、並列実行されるlinters/testers
    から呼ばれてもコマンド単位のグルーピングが崩れない。
    """
    lines = build_command_lines(result, config)
    with _write_lock:
        for line in lines:
            pyfltr.cli.output_format.structured_logger.info(line)


def write_jsonl_footer(
    results: list[pyfltr.command.CommandResult],
    *,
    exit_code: int,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
    fully_excluded_files: list[str] | None = None,
) -> None:
    """warning行+summary行を構造化出力loggerに書き出す。

    `results`は`_build_summary_record()`の集計に使用する。
    `run_id`と`launcher_prefix`は`summary.guidance`の起動コマンド整形に使う。
    `fully_excluded_files`を渡すと`summary.fully_excluded_files`キーとして埋め込む。
    """
    with _write_lock:
        for warning in warnings or []:
            pyfltr.cli.output_format.structured_logger.info(_dump(_build_warning_record(warning)))
        pyfltr.cli.output_format.structured_logger.info(
            _dump(
                _build_summary_record(
                    results,
                    exit_code=exit_code,
                    run_id=run_id,
                    launcher_prefix=launcher_prefix,
                    fully_excluded_files=fully_excluded_files,
                )
            )
        )


def _dump(record: dict[str, typing.Any]) -> str:
    """JSON 1行にシリアライズする。ensure_ascii=False + 区切り最短化でトークン効率を稼ぐ。"""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def _build_header_record(
    commands: list[str],
    files: int,
    *,
    run_id: str | None = None,
    mise_active_tools: dict[str, typing.Any] | None = None,
    format_source: str | None = None,
) -> dict[str, typing.Any]:
    """実行環境の基本情報をheaderレコードdictとして返す。

    `commands`は「実際に実行されるツール集合」（`--only-failed`やdisabledツール除外後）を
    前提とする。呼び出し側で絞り込み済みの配列を渡すこと。
    `mise_active_tools`は`{"status": ..., "detail": ..., "active_keys": [...]}`形式で渡し、
    指定時のみheaderへ露出する（mise経路を使うrunの自己診断用途）。
    `format_source`は`pyfltr.cli.FORMAT_SOURCE_*`の値で、出力形式の解決経路を明示する。
    指定時のみheaderへ露出する。
    """
    record: dict[str, typing.Any] = {
        "kind": "header",
        "version": importlib.metadata.version("pyfltr"),
        "python": sys.version,
        "executable": sys.executable,
        "platform": sys.platform,
        "cwd": os.getcwd(),
        "files": files,
        "commands": commands,
    }
    if run_id is not None:
        record["run_id"] = run_id
    if mise_active_tools is not None:
        record["mise_active_tools"] = mise_active_tools
    if format_source is not None:
        record["format_source"] = format_source
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
    """ErrorLocationを集約`messages[]`要素のdictに変換する。

    フィールド順は`line` → `col` → `end_line` → `end_col` → `rule` →
    `severity` → `fix` → `msg`。
    `rule_url`・`hint`は含めず、それぞれtoolレコードの`hint_urls`・`hints`へ集約する。
    Noneのフィールドは出力しない（`msg`は常に出力）。
    現状`end_line`/`end_col`を詰めるのはtextlintのみ。
    """
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


def _build_command_record(
    result: pyfltr.command.CommandResult,
    *,
    diagnostics: int,
    diagnostic_total: int | None = None,
    config: pyfltr.config.config.Config | None = None,
    hint_urls: dict[str, str] | None = None,
    hints: dict[str, str] | None = None,
) -> dict[str, typing.Any]:
    """CommandResultをcommandレコードdictに変換する。

    `diagnostics`は集約前の個別指摘件数（messages合計）を指定する。
    `failed`かつ`diagnostics == 0`のときに限り、`CommandResult.output`の末尾を
    `_truncate_message()`でトリムして`message`フィールドを付与する。
    メッセージ切り詰めまたはdiagnostic切り詰めが発生した場合は`truncated`メタを
    添付する。retry_commandは`CommandResult.retry_command`が設定されていれば含める。
    `hint_urls`が非空なら`hint_urls`キーで埋め込む。
    `hints`が非空なら`hints`キーで埋め込む。textlintコマンドの場合は
    `hints`の内容にかかわらず`messages[].end_col`の仕様を必ず追加する。
    `result.cached`が真のときは`elapsed`ではなく`cached_elapsed`キーに
    リネームして出力する（実行をスキップした前回値である旨をLLMに明示するため）。
    """
    record: dict[str, typing.Any] = {
        "kind": "command",
        "command": result.command,
        "type": result.command_type,
        "status": result.status,
        "files": result.files,
        "diagnostics": diagnostics,
    }
    # cached=Trueのときは実行をスキップしているため`elapsed`は出力せず
    # 前回実行時の計測値を`cached_elapsed`として提示する。LLMが「今回の実行時間」と
    # 誤解するのを避けるため両者を同時に出さない設計。
    elapsed_key = "cached_elapsed" if result.cached else "elapsed"
    record[elapsed_key] = round(result.elapsed, 2)
    if result.returncode is not None:
        record["rc"] = result.returncode

    truncated: dict[str, typing.Any] = {}
    archive_command_dir = pyfltr.paths.sanitize_command_name(result.command)
    if diagnostic_total is not None and diagnostic_total > diagnostics:
        truncated["diagnostics_total"] = diagnostic_total
        truncated["archive"] = f"tools/{archive_command_dir}/diagnostics.jsonl"

    if result.status in {"failed", "resolution_failed"} and diagnostics == 0:
        message_max_lines, message_max_chars = _resolve_message_limits(config)
        message, msg_truncated, head_chars, tail_chars = _truncate_message(
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
            truncated["head_chars"] = head_chars
            truncated["tail_chars"] = tail_chars
            truncated.setdefault("archive", f"tools/{archive_command_dir}/output.log")

    if truncated:
        record["truncated"] = truncated
    if result.retry_command is not None:
        record["retry_command"] = result.retry_command
    # ファイルhashキャッシュ（v3.0.0 パートD）。
    # `cached=True`のときはツール実行がスキップされ過去結果を復元したことを示す。
    # `cached_from`は復元元のrun_id（ULID）で、show-run / MCPから全文参照できる。
    if result.cached:
        record["cached"] = True
        if result.cached_from is not None:
            record["cached_from"] = result.cached_from
    if hint_urls:
        record["hint_urls"] = dict(hint_urls)
    # textlintコマンドはend_colをノード先頭からの累積位置で返す仕様のため、
    # rule hint有無にかかわらず必ずhints辞書にその旨を追加する。
    merged_hints = dict(hints) if hints else {}
    if result.command == "textlint":
        merged_hints["messages[].end_col"] = (
            "textlint reports end_col as cumulative offset from the text-node start, not in-line offset"
        )
    # formatterによる書き換えはそれ自体が成功扱いで、利用者・LLMエージェントが
    # 「再実行して直さなければならない」と誤解しないようコマンド単独の文脈ヒントを添える。
    # `summary.guidance`はパイプライン全体での総括として再実行不要に触れているが、
    # ここでは1コマンドの読み取りで結論まで到達できるように粒度を分けて並存させる。
    if result.status == "formatted":
        merged_hints["status.formatted"] = (
            "formatter rewrote files; rerun is not required because the rewrite itself counts as success"
        )
    if merged_hints:
        record["hints"] = merged_hints
    return record


def _resolve_message_limits(config: pyfltr.config.config.Config | None) -> tuple[int, int]:
    """tool.messageの行数・文字数上限をconfigから取得する。

    設定未指定時はパートC以前のハードコード値（30行 / 2000文字）を踏襲する。
    """
    if config is None:
        return 30, 2000
    max_lines = int(config.values.get("jsonl-message-max-lines", 30) or 0)
    max_chars = int(config.values.get("jsonl-message-max-chars", 2000) or 0)
    return max_lines, max_chars


def _build_summary_guidance(
    *,
    failure_present: bool,
    applied_fixes_present: bool,
    run_id: str | None,
    launcher_prefix: list[str] | None,
) -> list[str]:
    """summaryレコード向けの状況依存ガイドを英語で生成する。

    `summary.guidance`として次のいずれかを満たす場合のみ同梱する。

    - `commands_summary.needs_action`配下の`failed`/`resolution_failed`合計が1以上
    - `summary.applied_fixes`が非空（formatter/fix-stageが書き換えた）

    両方該当する場合は失敗時の4項目に続けてformatter書き換えの注記1項目を追記する。
    `run_id`と`launcher_prefix`が指定されていれば、起動コマンド表記と実run_idを埋め込む。
    未指定時はプレースホルダー（`<run_id>`）・既定値（`pyfltr`）にフォールバックする。
    """
    if not failure_present and not applied_fixes_present:
        return []
    launcher = shlex.join(launcher_prefix) if launcher_prefix else "pyfltr"
    items: list[str] = []
    if failure_present:
        run_id_token = run_id if run_id is not None else "<run_id>"
        items.extend(
            [
                "Inspect command.retry_command in failed command records to re-run only failing files.",
                f"Use '{launcher} run-for-agent --only-failed' to retry the failure set in one step.",
                "diagnostic.fix == 'safe'/'unsafe'/'suggested' means auto-fixable; 'none' or omitted means manual fix needed.",
                f"Use '{launcher} show-run {run_id_token}' for full per-command output stored in the run archive.",
            ]
        )
    if applied_fixes_present:
        items.append(
            "formatter/fix-stage rewrote files;"
            " re-running is not required because formatter rewrites are classified as no_issues by project policy."
        )
    return items


def _build_summary_record(
    ordered_results: list[pyfltr.command.CommandResult],
    *,
    exit_code: int,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
    fully_excluded_files: list[str] | None = None,
) -> dict[str, typing.Any]:
    """ordered_resultsから集計してsummaryレコードdictを作る。

    集計カウンタはコマンド単位の集計であることを示す`commands_summary`配下にまとめ、
    その下で「対応不要」「対応要」の2グループへネストする。
    `commands_summary.no_issues`配下に`succeeded`/`formatted`/`skipped`を、
    `commands_summary.needs_action`配下に`failed`を並べる。
    `formatted`は本リポジトリの運用上「再実行や追加対応は原則不要」と整理し
    `no_issues`側に分類する。
    `resolution_failed`は0件のときキー自体を省略する（通常プロジェクトでは常に0のため）。
    `failed`は0件でも常時出力する（0件であることがエラー無し判定に直結するため）。
    `commands_summary`の兄弟である`applied_fixes`/`fully_excluded_files`/
    `guidance`はカウント集計ではないため移動しない。
    `fully_excluded_files`が非空のとき、直接指定されたがexcludeパターン・.gitignore
    によって全除外されたファイル一覧を`fully_excluded_files`キーに埋め込む。
    exitコードは0のままだが、LLM/利用者が「警告ゼロ」と誤解しないよう明示する。
    `applied_fixes`はfixステージ・formatterステージで実際に内容変化したファイルパスを
    全コマンドにわたってユニオンしソートした一覧。変化なしの場合は省略する。
    """
    counts = {"succeeded": 0, "formatted": 0, "failed": 0, "resolution_failed": 0, "skipped": 0}
    total_diagnostics = 0
    fixed_files_union: set[str] = set()
    for result in ordered_results:
        counts[result.status] = counts.get(result.status, 0) + 1
        total_diagnostics += len(result.errors)
        if result.fixed_files:
            fixed_files_union.update(result.fixed_files)
    needs_action: dict[str, typing.Any] = {"failed": counts["failed"]}
    # resolution_failedはツール解決失敗時のみカウントされ、通常プロジェクトでは常に0。
    # 0件は情報として冗長なためキー自体を省略する。failedは0件でも常時出力する。
    if counts["resolution_failed"] > 0:
        needs_action["resolution_failed"] = counts["resolution_failed"]
    record: dict[str, typing.Any] = {
        "kind": "summary",
        "total": len(ordered_results),
        "commands_summary": {
            "no_issues": {
                "succeeded": counts["succeeded"],
                "formatted": counts["formatted"],
                "skipped": counts["skipped"],
            },
            "needs_action": needs_action,
        },
        "diagnostics": total_diagnostics,
        "exit": exit_code,
    }
    guidance = _build_summary_guidance(
        failure_present=counts["failed"] + counts["resolution_failed"] > 0,
        applied_fixes_present=bool(fixed_files_union),
        run_id=run_id,
        launcher_prefix=launcher_prefix,
    )
    if guidance:
        record["guidance"] = guidance
    if fixed_files_union:
        record["applied_fixes"] = sorted(fixed_files_union)
    if fully_excluded_files:
        record["fully_excluded_files"] = list(fully_excluded_files)
    return record


def _truncate_message(
    output: str,
    *,
    max_lines: int,
    max_chars: int,
    archived: bool,
) -> tuple[str, bool, int, int]:
    r"""生出力をハイブリッド方式（先頭 + 中略マーカー + 末尾）でトリムする。

    戻り値は`(切り詰め後メッセージ, 切り詰め発生したか, head_chars, tail_chars)`。
    `head_chars`/`tail_chars`は切り詰めが発生した場合の先頭・末尾ブロックの文字数。
    切り詰めが発生しなかった場合は両方0を返す。
    空文字は`("", False, 0, 0)`を返す（呼び出し側でmessageキーごと省略する）。
    `archived`が`False`の場合は切り詰めを行わず全文を返す（アーカイブから
    復元不能な情報欠落を避けるため）。

    ハイブリッド方式は冒頭側にエラー概要を出すツール（editorconfig-checker等）と
    末尾側にエラー詳細を出すツール（pytest・mypy等）の双方を救うことを意図する。
    `max_chars`を`head : tail = 1 : 4`（`_DEFAULT_HEAD_RATIO`）で配分し、
    `max_lines`は末尾側に対してのみ適用する（先頭側は文字数で十分に絞られるため）。
    `max_lines`/`max_chars`が0以下の場合は当該軸の切り詰めを行わない。
    """
    if not output:
        return "", False, 0, 0
    if not archived:
        return output, False, 0, 0

    line_count = output.count("\n") + (0 if output.endswith("\n") else 1)
    needs_truncate_chars = 0 < max_chars < len(output)
    needs_truncate_lines = 0 < max_lines < line_count
    if not needs_truncate_chars and not needs_truncate_lines:
        return output, False, 0, 0

    # ハイブリッド配分: 文字数制限がある場合はそれを基準にし、ない場合は十分大きな値とみなす。
    char_budget = max_chars if max_chars > 0 else len(output)
    head_size = max(1, int(char_budget * _DEFAULT_HEAD_RATIO))
    tail_size = char_budget - head_size
    # マーカー長を控除して合計上限を超えないようにする。
    overhead = len(_TRUNCATED_MARKER)
    if tail_size > overhead:
        tail_size -= overhead

    head_block = output[:head_size]
    tail_block = output[-tail_size:] if tail_size < len(output) else output

    # 末尾側のみ行数制限を追加適用する（先頭側は冒頭の要約行を素直に保持するため不要）。
    if max_lines > 0:
        tail_lines = tail_block.splitlines()
        if len(tail_lines) > max_lines:
            tail_block = "\n".join(tail_lines[-max_lines:])

    return head_block + _TRUNCATED_MARKER + tail_block, True, len(head_block), len(tail_block)
