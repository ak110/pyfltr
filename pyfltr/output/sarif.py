import importlib.metadata
import typing

import pyfltr.command.core_
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.diagnostics
import pyfltr.output.positions
import pyfltr.parsing.entry
import pyfltr.tools

_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA = "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0-rtm.5.json"

_SEVERITY_TO_LEVEL: dict[str | None, str] = {
    "error": "error",
    "warning": "warning",
    "info": "note",
}


def build_sarif(
    results: list[pyfltr.command.core_.CommandResult],
    config: pyfltr.config.model.Config,
    *,
    exit_code: int,
    commands: list[str] | None = None,
    files: int | None = None,
    run_id: str | None = None,
) -> dict[str, typing.Any]:
    ordered = sorted(results, key=lambda r: pyfltr.tools.command_index(config.command_names, r.command))

    sarif_runs: list[dict[str, typing.Any]] = []
    for result in ordered:
        sarif_runs.append(_build_run(result))

    return {
        "$schema": _SARIF_SCHEMA,
        "version": _SARIF_VERSION,
        "runs": sarif_runs,
        "properties": {
            "pyfltr": {
                "version": importlib.metadata.version("pyfltr"),
                "exit_code": exit_code,
                "commands": commands or [],
                "files": files,
                "run_id": run_id,
            },
        },
    }


def _build_run(result: pyfltr.command.core_.CommandResult) -> dict[str, typing.Any]:
    rule_index: dict[str, int] = {}
    rules: list[dict[str, typing.Any]] = []
    for error in result.errors:
        if error.rule is None or error.rule in rule_index:
            continue
        rule_index[error.rule] = len(rules)
        rule_obj: dict[str, typing.Any] = {"id": error.rule}
        if error.rule_url is not None:
            rule_obj["helpUri"] = error.rule_url
        rules.append(rule_obj)

    sarif_results: list[dict[str, typing.Any]] = []
    for error in result.errors:
        sarif_results.append(_build_result_record(error, rule_index))

    invocation: dict[str, typing.Any] = {"executionSuccessful": not result.failed}
    if result.retry_command is not None:
        invocation["commandLine"] = result.retry_command

    return {
        "tool": {
            "driver": {
                "name": result.command,
                "rules": rules,
            }
        },
        "invocations": [invocation],
        "results": sarif_results,
    }


def _build_result_record(
    error: pyfltr.diagnostics.ErrorLocation,
    rule_index: dict[str, int],
) -> dict[str, typing.Any]:
    region: dict[str, typing.Any] = {"startLine": error.line}
    if pyfltr.output.positions.is_publishable_column(error, error.col):
        region["startColumn"] = error.col
    if error.end_line is not None:
        region["endLine"] = error.end_line
    if pyfltr.output.positions.is_publishable_column(error, error.end_col):
        region["endColumn"] = error.end_col
    record: dict[str, typing.Any] = {
        "level": _SEVERITY_TO_LEVEL.get(error.severity, "warning"),
        "message": {"text": error.message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": error.file},
                    "region": region,
                }
            }
        ],
    }
    if error.rule is not None:
        record["ruleId"] = error.rule
        if error.rule in rule_index:
            record["ruleIndex"] = rule_index[error.rule]
    return record
