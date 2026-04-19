"""pytest 共通定義。

`CommandResult` や `ErrorLocation` のダミーを生成するヘルパーと、
実行アーカイブのテストデータ生成ヘルパーを集約する。
各テストファイルで同じようなビルダーを書き散らかすと pylint の
duplicate-code (R0801) に掛かるため、ここに集約する。conftest.py に置くのは
pre-commit の name-tests-test フックから除外されるため。
"""

import pathlib

import pytest

import pyfltr.archive
import pyfltr.cache
import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.only_failed
import pyfltr.warnings_


@pytest.fixture(autouse=True)
def _clear_warnings_between_tests() -> None:
    """全テストで警告状態を持ち越さないため、各テスト開始前に蓄積をクリアする。

    ``pyfltr.warnings_`` はモジュール変数としてプロセス内で共有されるため、
    テスト間のリークが発生すると順序依存や並列実行での非決定性を招く。
    conftest.py で autouse 化することで、各テストが空状態から始まることを保証する。
    """
    pyfltr.warnings_.clear()


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


def make_execution_context(
    config: pyfltr.config.Config,
    all_files: list[pathlib.Path],
    *,
    cache_store: pyfltr.cache.CacheStore | None = None,
    cache_run_id: str | None = None,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.only_failed.ToolTargets | None = None,
) -> pyfltr.command.ExecutionContext:
    """テスト用の ExecutionContext を生成する。

    ``execute_command`` を直接呼び出すテストで使用する。
    CLI/TUI フック系（on_output / is_interrupted / on_subprocess_start / on_subprocess_end）は
    テストでは不要なため省略（デフォルトの None が使われる）。
    """
    base = pyfltr.command.ExecutionBaseContext(
        config=config,
        all_files=all_files,
        cache_store=cache_store,
        cache_run_id=cache_run_id,
    )
    return pyfltr.command.ExecutionContext(
        base=base,
        fix_stage=fix_stage,
        only_failed_targets=only_failed_targets,
    )


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
    archived: bool = True,
    retry_command: str | None = None,
    cached: bool = False,
    cached_from: str | None = None,
    target_files: list[pathlib.Path] | None = None,
) -> pyfltr.command.CommandResult:
    """テスト用の CommandResult を生成する。

    ``has_error`` を省略した場合、``returncode`` が 0/None 以外なら True に推定する。
    ``errors`` は ``ErrorLocation`` のリスト (省略時は空)。``target_files`` は
    ``retry_command`` 絞り込み (A案) のテスト用 (省略時は空)。
    ``archived`` はテスト既定で True (smart truncation が適用される側)。
    実運用でのデフォルト (CommandResult() 生成時の False) とは異なる点に注意。
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
        target_files=list(target_files) if target_files else [],
        archived=archived,
        retry_command=retry_command,
        cached=cached,
        cached_from=cached_from,
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


def seed_archive_run(
    cache_root: pathlib.Path,
    *,
    commands: list[str] | None = None,
    files: int = 3,
    exit_code: int = 0,
    tool_results: list[tuple[str, int, str, list]] | None = None,
) -> str:
    """テスト用の run をアーカイブに書き込み、``run_id`` を返す。

    ``tool_results`` は ``(tool, returncode, output, errors)`` のタプル列。
    ``runs_test`` / ``mcp_test`` 等で同じセットアップ手順を踏むため、
    duplicate-code (R0801) 回避用に conftest.py 側へ集約している。
    """
    store = pyfltr.archive.ArchiveStore(cache_root=cache_root)
    run_id = store.start_run(commands=commands or ["ruff-check"], files=files)
    for tool, returncode, output, errors in tool_results or []:
        result = make_command_result(tool, returncode=returncode, output=output, errors=errors)
        store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=exit_code, commands=commands, files=files)
    return run_id
