import pyfltr.output.github_annotations
import pyfltr.paths
import pyfltr.rule_urls
from pyfltr.diagnostics import ErrorLocation


def format_error(error: ErrorLocation) -> str:
    col_str = f":{error.col}" if error.col else ""
    tag = f"{error.command}:{error.rule}" if error.rule else error.command
    message = f"[INFO] {error.message}" if error.severity == "info" else error.message
    return f"{error.file}:{error.line}{col_str}: [{tag}] {message}"


def format_error_github(error: ErrorLocation) -> str:
    return pyfltr.output.github_annotations.build_workflow_command(error)
