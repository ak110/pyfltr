"""LLM向けJSON Lines出力。

`--output-format=jsonl`で呼ばれ、CommandResult群をLLM / エージェントが
読みやすいフラットなJSON Lines形式（header / diagnostic / command / warning / summaryの5種別）に
変換して出力する。

`diagnostic`は`(command, file)`単位で集約し、個別指摘は`messages[]`に格納する。
ルールURLはcommand単位の`hint_urls`辞書へ、ルール別の短い修正ヒントは`hints`辞書へそれぞれ集約し、
LLM入力時のトークン浪費を抑える。
"""

import typing

import pyfltr.command.completion
import pyfltr.command.core_
import pyfltr.config.model
import pyfltr.output.jsonl_emit
import pyfltr.output.jsonl_records
import pyfltr.parsing.entry
import pyfltr.tools


def build_command_lines(
    result: pyfltr.command.core_.CommandResult,
    config: pyfltr.config.model.Config,
    *,
    quiet: bool = False,
) -> list[str]:
    """1コマンド分のdiagnostic行+command行をJSONL文字列のリストとして生成する。

    diagnostic行は`(command, file)`単位で集約され、個別指摘は`messages[]`に並ぶ。
    個別指摘の合計件数が`jsonl-diagnostic-limit`を超える場合は
    `ErrorLocation`列を先頭N件に限定してから集約し、
    commandレコードに`truncated.diagnostics_total`を添付する。
    切り詰めは`result.archived`がTrueのときのみ適用し、Falseの場合は全件出力する
    （アーカイブから復元不能な情報欠落を防ぐため）。

    判定単位はステージごとの`CommandResult`単位とする。fixステージと
    通常ステージは同じ`command`名で別`CommandResult`として渡されるため、
    片方のアーカイブ書き込み失敗が他方の切り詰め可否に影響しない。

    `quiet=True`のときは以下の全条件を満たす成功時commandレコードを出力しない
    （SSOT: 本docstring）。

    - `result.status`が`succeeded`・`formatted`・`skipped`のいずれか
    - `diagnostic_records`が空
    - `diagnostics_truncated`が`False`
    - `result.slow_tests`が空
    - `result.runner_fallback`が`None`（fallback検知は静音時も残す）

    上記いずれかを満たさない場合は通常どおりcommandレコードを出力する。
    `diagnostic_records`自体は`quiet`の影響を受けず、非空なら常に出力する。
    """
    sorted_errors = pyfltr.parsing.entry.sort_errors(result.errors, config.command_names)
    diagnostic_total = len(sorted_errors)
    diagnostic_limit = int(config.values.get("jsonl-diagnostic-limit", 0) or 0)

    diagnostics_truncated = False
    if 0 < diagnostic_limit < diagnostic_total and result.archived:
        sorted_errors = sorted_errors[:diagnostic_limit]
        diagnostics_truncated = True

    diagnostic_records, hint_urls, hints = pyfltr.output.jsonl_records.aggregate_diagnostics(sorted_errors)

    lines: list[str] = []
    for record in diagnostic_records:
        lines.append(pyfltr.output.jsonl_records.dump_record(record))
    if quiet and _should_suppress_command_record(
        result,
        diagnostic_records=diagnostic_records,
        diagnostics_truncated=diagnostics_truncated,
    ):
        return lines
    lines.append(
        pyfltr.output.jsonl_records.dump_record(
            pyfltr.output.jsonl_records.build_command_record(
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


def _should_suppress_command_record(
    result: pyfltr.command.core_.CommandResult,
    *,
    diagnostic_records: list[dict[str, typing.Any]],
    diagnostics_truncated: bool,
) -> bool:
    """`quiet`時にcommandレコード出力を抑止するべきか判定する。

    抑止条件は`build_command_lines`のdocstringを参照する（SSOT）。
    """
    if result.needs_rerun:
        return False
    if diagnostic_records:
        return False
    if diagnostics_truncated:
        return False
    if result.slow_tests:
        return False
    return result.runner_fallback is None


def build_lines(
    results: list[pyfltr.command.core_.CommandResult],
    config: pyfltr.config.model.Config,
    *,
    exit_code: int,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
    subcommand: str | None = None,
    fully_excluded_files: list[str] | None = None,
    missing_targets: list[str] | None = None,
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
    `subcommand`は`summary.guidance`の再実行例に埋め込む実行系サブコマンド名。
    """
    ordered = sorted(results, key=lambda r: pyfltr.tools.command_index(config.command_names, r.command))

    lines: list[str] = []

    if commands is not None and files is not None:
        lines.append(
            pyfltr.output.jsonl_records.dump_record(
                pyfltr.output.jsonl_records.build_header_record(
                    commands,
                    files,
                    run_id=run_id,
                    mise_active_tools=pyfltr.output.jsonl_records.collect_mise_active_tools_for_header(commands, config),
                    format_source=format_source,
                )
            )
        )

    for warning in warnings or []:
        lines.append(pyfltr.output.jsonl_records.dump_record(pyfltr.output.jsonl_records.build_warning_record(warning)))

    for result in ordered:
        lines.extend(build_command_lines(result, config))

    lines.append(
        pyfltr.output.jsonl_records.dump_record(
            pyfltr.output.jsonl_records.build_summary_record(
                ordered,
                exit_code=exit_code,
                run_id=run_id,
                launcher_prefix=launcher_prefix,
                subcommand=subcommand,
                fully_excluded_files=fully_excluded_files,
                missing_targets=missing_targets,
                warning_count=len(warnings or []),
            )
        )
    )
    return lines


def write_jsonl_header(
    commands: list[str],
    files: int,
    *,
    run_id: str | None = None,
    config: pyfltr.config.model.Config | None = None,
    format_source: str | None = None,
    quiet: bool = False,
) -> None:
    """header行を構造化出力loggerに出力する（ストリーミングモード用）。

    パイプライン開始直後、diagnostic行より前に1回だけ呼ぶ。`run_id`が指定されていれば
    headerレコードに含める（アーカイブ参照時の識別キー）。
    出力先は`pyfltr.output.logging_.configure_structured_output()`が設定したhandlerに従う
    （stdoutもしくは`--output-file`のFileHandler）。
    `config`が渡された場合、mise経路を使うrunに限り
    `mise_active_tools`フィールドへ取得状況を露出する。
    `format_source`が渡された場合、`format_source`フィールドへ解決経路ラベルを露出する。
    `quiet=True`のときは`run_id`・`commands`・`files`の3つのフィールドのみへ縮約する
    （SSOT: `pyfltr.output.jsonl_records.build_header_record`のdocstring）。
    """
    if quiet:
        mise_active_tools = None
    else:
        mise_active_tools = (
            pyfltr.output.jsonl_records.collect_mise_active_tools_for_header(commands, config) if config is not None else None
        )
    pyfltr.output.jsonl_emit.emit_record(
        pyfltr.output.jsonl_records.dump_record(
            pyfltr.output.jsonl_records.build_header_record(
                commands,
                files,
                run_id=run_id,
                mise_active_tools=mise_active_tools,
                format_source=format_source,
                quiet=quiet,
            )
        )
    )


def write_jsonl_streaming(
    result: pyfltr.command.core_.CommandResult,
    config: pyfltr.config.model.Config,
    *,
    quiet: bool = False,
) -> None:
    """1コマンド分のdiagnostic行+command行を構造化出力loggerに即時出力する。

    `pyfltr.output.jsonl_emit._write_lock`取得下で複数行を連続出力することで、並列実行されるlinters/testers
    から呼ばれてもコマンド単位のグルーピングが崩れない。
    `quiet=True`は`build_command_lines`へ透過する（成功時commandレコードを抑止する）。
    """
    lines = build_command_lines(result, config, quiet=quiet)
    pyfltr.output.jsonl_emit.emit_records(lines)


def write_jsonl_running_event(command: str, elapsed: float) -> None:
    """heartbeat由来の `status:"running"` レコードを構造化出力loggerに即時出力する。

    `command`は実行中コマンド名、`elapsed`はパイプライン側で追跡している
    `start_time` からの経過秒数（壁時計）。
    フィールド名は最終commandレコードの`elapsed`と揃え、LLMの自己説明性を高める。
    パイプライン全体の「最後のJSONL出力からの無音時間」がしきい値を超えた際、
    実行中コマンドそれぞれについて1件ずつ呼び出される設計。
    `pyfltr.output.jsonl_emit._emit_structured` 経由で `last_jsonl_output_time` も更新するため、
    heartbeat発火直後の連続発火は自動的に抑止される。
    """
    record: dict[str, typing.Any] = {
        "kind": "command",
        "command": command,
        "status": "running",
        "elapsed": round(elapsed, 2),
    }
    pyfltr.output.jsonl_emit.emit_record(pyfltr.output.jsonl_records.dump_record(record))


def write_jsonl_footer(
    results: list[pyfltr.command.core_.CommandResult],
    *,
    exit_code: int,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
    subcommand: str | None = None,
    fully_excluded_files: list[str] | None = None,
    missing_targets: list[str] | None = None,
    completion: pyfltr.command.completion.RunCompletion | None = None,
) -> None:
    """warning行+summary行を構造化出力loggerに出力する。

    `results`は`pyfltr.output.jsonl_records.build_summary_record()`の集計に使用する。
    `run_id`と`launcher_prefix`は`summary.guidance`の起動コマンド整形に使う。
    `subcommand`は`summary.guidance`の再実行例に埋め込む実行系サブコマンド名。
    `fully_excluded_files`を渡すと`summary.fully_excluded_files`キーとして埋め込む。
    `missing_targets`を渡すと`summary.missing_targets`キーとして埋め込む。
    `completion`を渡すと完了判定の4項目を`summary`へ埋め込む。
    `warnings`の件数は`summary.warnings`としても集計する。
    """
    lines = [
        pyfltr.output.jsonl_records.dump_record(pyfltr.output.jsonl_records.build_warning_record(warning))
        for warning in warnings or []
    ]
    lines.append(
        pyfltr.output.jsonl_records.dump_record(
            pyfltr.output.jsonl_records.build_summary_record(
                results,
                exit_code=exit_code,
                run_id=run_id,
                launcher_prefix=launcher_prefix,
                subcommand=subcommand,
                fully_excluded_files=fully_excluded_files,
                missing_targets=missing_targets,
                warning_count=len(warnings or []),
                completion=completion,
            )
        )
    )
    pyfltr.output.jsonl_emit.emit_records(lines)
