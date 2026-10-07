"""診断のテキスト・GitHub表示。"""

import pyfltr.output.github_annotations
import pyfltr.paths
import pyfltr.rule_urls
from pyfltr.diagnostics import ErrorLocation


def format_error(error: ErrorLocation) -> str:
    """エラー箇所を`file:line[:col]: [tool[:rule]] message`のテキスト形式にフォーマットする。

    `severity == "info"`のときはmessage先頭に`[INFO] `を付加し、
    エラーや警告と視覚的に区別する。warning severityは既存のテキスト出力との
    互換性を維持するため表記を変更しない。
    """
    col_str = f":{error.col}" if error.col else ""
    tag = f"{error.command}:{error.rule}" if error.rule else error.command
    message = f"[INFO] {error.message}" if error.severity == "info" else error.message
    return f"{error.file}:{error.line}{col_str}: [{tag}] {message}"


def format_error_github(error: ErrorLocation) -> str:
    """エラー箇所をGitHub Actionsのワークフローコマンド記法にフォーマットする。

    `::error file=...::message`形式で出力する。
    """
    return pyfltr.output.github_annotations.build_workflow_command(error)
