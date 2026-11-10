"""解析テストの共通補助。"""

import pyfltr.diagnostics


def diagnostic_fields(
    error: pyfltr.diagnostics.ErrorLocation,
) -> tuple[str, int, int | None, str, str, str | None, str | None, str | None, int | None, int | None]:
    """位置取り込み前から存在する診断情報と終了位置を比較用タプルで返す。"""
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


def pytest_child_run(*, file: str, test: str, line: int, message: str, terminated: bool = True) -> str:
    """捕捉出力へ混入する子プロセスのpytest実行1回分を組み立てる。

    実出力と同じく`test session starts`見出しで始まり、終了集計行で終わる形とする。
    除外の判定は対象の2つのマーカーに依存するため、テスト入力からいずれも省略しない。
    `terminated=False`は子プロセスが異常終了・打ち切りで終了集計行を欠く形を表す。
    """
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
