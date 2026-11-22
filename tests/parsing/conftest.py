import pyfltr.diagnostics


def diagnostic_fields(
    error: pyfltr.diagnostics.ErrorLocation,
) -> tuple[str, int, int | None, str, str, str | None, str | None, str | None, int | None, int | None]:
    return (
        error.file,
        error.line,
        error.col,
        error.command,
        error.message,
        error.rule,
        error.severity,
        error.fix,
        error.end_line,
        error.end_col,
    )


def child_pytest_run(*, file: str, test: str, line: int, message: str, terminated: bool = True) -> str:
    body = "================================= test session starts =================================\ncollected 1 item\n\n"
    if not terminated:
        return body + f"{file} \n"
    return (
        body + "================================= FAILURES =================================\n"
        f"_______________________________ {test} ________________________________\n"
        f"{file}:{line}: in {test}\n"
        f"E   {message}\n"
        "========================= short test summary info ==========================\n"
        f"FAILED {file}::{test} - {message}\n"
        "================================= 1 failed in 0.04s =================================\n"
    )
