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
    lines = build_command_lines(result, config, quiet=quiet)
    pyfltr.output.jsonl_emit.emit_records(lines)


def write_jsonl_running_event(command: str, elapsed: float) -> None:
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
