"""pytest 共通定義。

`CommandResult` や `ErrorLocation` のダミーを生成するヘルパーを集約する。
各テストファイルで同じようなビルダーを書き散らかすと pylint の
duplicate-code (R0801) に掛かるため、ここに集約する。conftest.py に置くのは
pre-commit の name-tests-test フックから除外されるため。
"""

import pyfltr.command
import pyfltr.error_parser

# Rust / .NET 言語ツールの既定値定数。config_test / command_test の両方が
# cargo-clippy の args / fix-args を参照するため、ここで一元管理する。
CARGO_CLIPPY_ARGS: list[str] = ["clippy", "--all-targets"]
CARGO_CLIPPY_LINT_ARGS: list[str] = ["--", "-D", "warnings"]
CARGO_CLIPPY_FIX_ARGS: list[str] = [
    "--fix",
    "--allow-staged",
    "--allow-dirty",
    "--",
    "-D",
    "warnings",
]
CARGO_CLIPPY_LINT_CMDLINE: list[str] = ["cargo", *CARGO_CLIPPY_ARGS, *CARGO_CLIPPY_LINT_ARGS]
CARGO_CLIPPY_FIX_CMDLINE: list[str] = ["cargo", *CARGO_CLIPPY_ARGS, *CARGO_CLIPPY_FIX_ARGS]


def make_command_result(
    command: str,
    *,
    returncode: int | None,
    command_type: str = "linter",
    output: str = "",
    files: int = 1,
    elapsed: float = 0.1,
    errors: list[pyfltr.error_parser.ErrorLocation] | None = None,
    has_error: bool | None = None,
) -> pyfltr.command.CommandResult:
    """テスト用の CommandResult を生成する。

    `has_error` を省略した場合、`returncode` が 0/None 以外なら True に推定する。
    `errors` は `ErrorLocation` のリスト (省略時は空)。
    """
    if has_error is None:
        has_error = returncode is not None and returncode != 0
    return pyfltr.command.CommandResult(
        command=command,
        command_type=command_type,
        commandline=[command],
        returncode=returncode,
        has_error=has_error,
        files=files,
        output=output,
        elapsed=elapsed,
        errors=list(errors) if errors else [],
    )


def make_error_location(
    command: str,
    file: str,
    line: int,
    message: str,
    col: int | None = None,
) -> pyfltr.error_parser.ErrorLocation:
    """テスト用の ErrorLocation を生成する。"""
    return pyfltr.error_parser.ErrorLocation(
        file=file,
        line=line,
        col=col,
        command=command,
        message=message,
    )
