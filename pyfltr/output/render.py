import logging
import shlex
import typing
import uuid

import pyfltr.command.core_
import pyfltr.command.slow_tests
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.output.diagnostics
import pyfltr.output.logging_
import pyfltr.parsing.entry
import pyfltr.tools
import pyfltr.warnings_

NCOLS = 128

logger = logging.getLogger(__name__)

text_logger = pyfltr.output.logging_.text_logger
lock = pyfltr.output.logging_.text_output_lock


def _write_raw_output(output: str, *, use_github_annotations: bool) -> None:
    if use_github_annotations:
        token = uuid.uuid4().hex
        text_logger.info(f"::stop-commands::{token}")
        text_logger.info(output)
        text_logger.info(f"::{token}::")
    else:
        text_logger.info(output)


def write_log(result: pyfltr.command.core_.CommandResult, *, use_github_annotations: bool = False) -> None:
    mark = "@" if result.alerted else "*"
    with lock:
        text_logger.info(f"{mark * 32} {result.command} {mark * (NCOLS - 34 - len(result.command))}")
        logger.debug(f"{mark} commandline: {shlex.join(result.commandline)}")
        text_logger.info(mark)
        if result.errors:
            for error in result.errors:
                if use_github_annotations:
                    text_logger.info(pyfltr.output.diagnostics.format_error_github(error))
                else:
                    text_logger.info(pyfltr.output.diagnostics.format_error(error))
            if result.command_type == "tester" and result.alerted:
                _write_raw_output(result.output, use_github_annotations=use_github_annotations)
        elif result.alerted:
            _write_raw_output(result.output, use_github_annotations=use_github_annotations)
        else:
            summary = pyfltr.parsing.entry.parse_summary(result.command, result.output)
            if summary:
                text_logger.info(f"{mark} {summary}")
        for line in pyfltr.command.slow_tests.format_slow_tests(result.slow_tests):
            text_logger.info(f"{mark} {line}")
        text_logger.info(mark)
        text_logger.info(f"{mark} returncode: {result.returncode}")
        text_logger.info(mark * NCOLS)


def render_results(
    results: list[pyfltr.command.core_.CommandResult],
    config: pyfltr.config.model.Config,
    *,
    include_details: bool,
    output_format: str = "text",
    exit_code: int = 0,
    commands: list[str] | None = None,
    files: int | None = None,
    warnings: list[dict[str, typing.Any]] | None = None,
    run_id: str | None = None,
    launcher_prefix: list[str] | None = None,
) -> None:
    del exit_code, commands, files, run_id, launcher_prefix
    ordered = sorted(results, key=lambda r: pyfltr.tools.command_index(config.command_names, r.command))
    warnings = warnings or []

    use_ga = output_format == "github-annotations"
    if include_details:
        for result in ordered:
            if not result.alerted:
                write_log(result, use_github_annotations=use_ga)

        for result in ordered:
            if result.alerted:
                write_log(result, use_github_annotations=use_ga)

    _write_warnings_section(warnings)

    _write_missing_targets_section(pyfltr.warnings_.filtered_direct_files(reason="missing"))

    _write_fully_excluded_files_section(pyfltr.warnings_.filtered_direct_files(reason="excluded"))

    _write_summary(ordered)


def _write_warnings_section(warnings: list[dict[str, typing.Any]]) -> None:
    if not warnings:
        return
    with lock:
        text_logger.info(f"{'-' * 10} warnings {'-' * (72 - 10 - 10)}")
        for entry in warnings:
            text_logger.info(f"    [{entry['source']}] {pyfltr.warnings_.format_warning_text(entry)}")


def _write_missing_targets_section(files: list[str]) -> None:
    if not files:
        return
    with lock:
        text_logger.info(f"{'-' * 10} missing-targets {'-' * (72 - 10 - 17)}")
        for path in files:
            text_logger.info(f"    {path}")


def _write_fully_excluded_files_section(files: list[str]) -> None:
    if not files:
        return
    with lock:
        text_logger.info(f"{'-' * 10} fully-excluded-files {'-' * (72 - 10 - 22)}")
        for path in files:
            text_logger.info(f"    {path}")


def _write_summary(ordered_results: list[pyfltr.command.core_.CommandResult]) -> None:
    with lock:
        text_logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
        for result in ordered_results:
            text_logger.info(f"    {result.command:<16s} {result.get_status_text()}")
        text_logger.info("-" * 72)
