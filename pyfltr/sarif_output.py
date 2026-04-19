"""SARIF 2.1.0 形式の出力生成。

``--output-format=sarif`` で呼ばれ、CommandResult 群を SARIF 2.1.0 スキーマに沿った
dict に変換する。1 つの run オブジェクトあたり 1 ツールを対応付け、``rules`` に
重複なしで当該ツールが検出したルールを列挙する。``results`` 配列に diagnostic を
配置し、``level`` を pyfltr の severity 3 値から SARIF の 3 値 (``error`` / ``warning``
/ ``note``) に変換する。
"""

import importlib.metadata
import typing

import pyfltr.command
import pyfltr.config
import pyfltr.error_parser

_SARIF_VERSION = "2.1.0"
_SARIF_SCHEMA = "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0-rtm.5.json"

_SEVERITY_TO_LEVEL: dict[str | None, str] = {
    "error": "error",
    "warning": "warning",
    "info": "note",
}


def build_sarif(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    exit_code: int,
    commands: list[str] | None = None,
    files: int | None = None,
    run_id: str | None = None,
) -> dict[str, typing.Any]:
    """SARIF 2.1.0 互換の dict を生成する。

    SARIF 側は executionSuccessful を使って exit_code を反映する (exit_code == 0 なら
    True、それ以外は False)。retry_command は各 run の ``invocations[].commandLine`` に
    添付する。``commands`` / ``files`` / ``run_id`` は ``pyfltr`` プロパティとして
    保存し、SARIF 消費側が参考情報として利用できるようにする。
    """
    ordered = sorted(results, key=lambda r: _command_index(config, r.command))

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


def _build_run(result: pyfltr.command.CommandResult) -> dict[str, typing.Any]:
    """1 ツール分の run オブジェクトを組み立てる。"""
    # rules リスト (ruleId の重複を除去したうえで登録順を維持)。
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

    invocation: dict[str, typing.Any] = {"executionSuccessful": not result.has_error}
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
    error: pyfltr.error_parser.ErrorLocation,
    rule_index: dict[str, int],
) -> dict[str, typing.Any]:
    """1 diagnostic 分の SARIF result を生成する。"""
    region: dict[str, typing.Any] = {"startLine": error.line}
    if error.col is not None:
        region["startColumn"] = error.col
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


def _command_index(config: pyfltr.config.Config, command: str) -> int:
    """config.command_names 内での位置を返す（未登録コマンドは末尾扱い）。"""
    if command in config.command_names:
        return config.command_names.index(command)
    return len(config.command_names)
