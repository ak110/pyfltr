"""pytest 共通定義。

`CommandResult`・`ErrorLocation`・`ArchiveStore`のテストデータ生成ヘルパーを集約する。
各テストファイルに同種ビルダーを個別定義すると pylint の duplicate-code（R0801）に
抵触するため、conftest.py に集約する。conftest.py に置くことで
pre-commit の name-tests-test フックから除外される。
"""

import argparse
import pathlib
import time

import pytest

import pyfltr.command.core_
import pyfltr.command.error_parser
import pyfltr.command.mise
import pyfltr.config.config
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.warnings_


@pytest.fixture(autouse=True)
def _clear_warnings_between_tests() -> None:
    """各テスト開始前に警告蓄積をクリアするフィクスチャ。

    `pyfltr.warnings_`はプロセス内でモジュール変数として共有される。
    クリアしないとテスト間でリークが発生し、順序依存や並列実行での非決定性を招く。
    """
    pyfltr.warnings_.clear()


@pytest.fixture(autouse=True)
def _isolate_output_format_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """出力形式を左右する環境変数を未設定状態に固定するフィクスチャ。

    Claude Code などのエージェント環境では `AI_AGENT` が常時設定されるため、
    autouse しないと開発機の状態によって既定出力形式が jsonl へ揺らぐ。
    `PYFLTR_OUTPUT_FORMAT` も開発者シェルで設定されている可能性があるため、
    両者を一律に未設定化する。個別テストは `monkeypatch.setenv` で上書きする。
    """
    monkeypatch.delenv("AI_AGENT", raising=False)
    monkeypatch.delenv("PYFLTR_OUTPUT_FORMAT", raising=False)


# `get_mise_active_tools` 実装本体を保存しておく（モック上書き前の参照）。
# キャッシュキー検証系テストでは実装の挙動を直接確認したいため、モック前の関数オブジェクトを
# 経由して呼べるよう、本変数をテスト側から参照可能にする。
real_get_mise_active_tools = pyfltr.command.mise.get_mise_active_tools


@pytest.fixture(autouse=True)
def _default_mise_active_tools_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """`mise ls --current --json` 経路を「mise設定記述なし」相当へ固定するフィクスチャ。

    `get_mise_active_tools` は実環境の mise と cwd 配下の `mise.toml` に依存するため、
    モックしないと開発機の状態でテスト結果が揺らぐ（tool spec 省略形が混入する等）。
    既存テストは mise 設定記述なし前提で `mise exec <tool>@latest -- <bin>` 形式を期待する。
    新仕様（mise 設定記述あり時の tool spec 省略）を検証するテストは個別に上書きする。
    """
    monkeypatch.setattr("pyfltr.command.mise._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr(
        "pyfltr.command.mise.get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.mise.MiseActiveToolsResult(status="ok"),
    )


@pytest.fixture(autouse=True)
def _isolate_global_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> pathlib.Path:
    """`PYFLTR_GLOBAL_CONFIG` を tmp パス配下のダミーパスへ固定するフィクスチャ。

    開発機の `~/.config/pyfltr/config.toml` がテスト結果に影響しないようにする。
    個別テストは `monkeypatch.setenv` で上書きできる。
    """
    isolation_dir = tmp_path_factory.mktemp("global_config_isolation")
    target = isolation_dir / "config.toml"
    monkeypatch.setenv("PYFLTR_GLOBAL_CONFIG", str(target))
    return target


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
    config: pyfltr.config.config.Config,
    all_files: list[pathlib.Path],
    *,
    cache_store: pyfltr.state.cache.CacheStore | None = None,
    cache_run_id: str | None = None,
    fix_stage: bool = False,
    only_failed_targets: pyfltr.state.only_failed.ToolTargets | None = None,
) -> pyfltr.command.core_.ExecutionContext:
    """テスト用の ExecutionContext を生成する。

    `execute_command` を直接呼び出すテストで使用する。
    CLI/TUI フック系（on_output / is_interrupted / on_subprocess_start / on_subprocess_end）は
    省略し、デフォルトの None を使う。
    """
    base = pyfltr.command.core_.ExecutionBaseContext(
        config=config,
        all_files=all_files,
        cache_store=cache_store,
        cache_run_id=cache_run_id,
    )
    return pyfltr.command.core_.ExecutionContext(
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
    errors: list[pyfltr.command.error_parser.ErrorLocation] | None = None,
    has_error: bool | None = None,
    archived: bool = True,
    retry_command: str | None = None,
    cached: bool = False,
    cached_from: str | None = None,
    target_files: list[pathlib.Path] | None = None,
    resolution_failed: bool = False,
) -> pyfltr.command.core_.CommandResult:
    """テスト用の CommandResult を生成する。

    `has_error` を省略した場合、`returncode` が 0/None 以外なら True とする。
    `errors` は `ErrorLocation` のリスト（省略時は空）。
    `target_files` は `retry_command` フィルタリングのテスト用（省略時は空）。
    `archived` のテスト既定は True（smart truncation 適用側）。
    実運用の `CommandResult()` 生成時の既定（False）とは異なるため注意する。
    """
    if has_error is None:
        has_error = returncode is not None and returncode != 0
    return pyfltr.command.core_.CommandResult(
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
        resolution_failed=resolution_failed,
    )


def make_error_location(
    command: str,
    file: str,
    line: int,
    message: str,
    col: int | None = None,
) -> pyfltr.command.error_parser.ErrorLocation:
    """テスト用の ErrorLocation を生成する。"""
    return pyfltr.command.error_parser.ErrorLocation(
        file=file,
        line=line,
        col=col,
        command=command,
        message=message,
    )


def make_formatted_result(command: str = "ruff-format") -> pyfltr.command.core_.CommandResult:
    """`status == "formatted"` になる最小の CommandResult を生成する。"""
    return pyfltr.command.core_.CommandResult(
        command, "formatter", [command], returncode=1, has_error=False, files=1, output="", elapsed=0.01
    )


def make_succeeded_result(command: str = "ruff-check") -> pyfltr.command.core_.CommandResult:
    """`status == "succeeded"` になる最小の CommandResult を生成する。"""
    return pyfltr.command.core_.CommandResult(
        command, "linter", [command], returncode=0, has_error=False, files=1, output="", elapsed=0.01
    )


def make_archive_store(tmp_path: pathlib.Path) -> pyfltr.state.archive.ArchiveStore:
    """テスト用の`ArchiveStore`を生成する。"""
    return pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)


def make_args(*, no_exclude: bool = False) -> argparse.Namespace:
    """`execute_command`に渡す`argparse.Namespace`を生成する。"""
    return argparse.Namespace(shuffle=False, verbose=False, no_exclude=no_exclude)


@pytest.fixture
def _isolated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    """`PYFLTR_CACHE_DIR`を`tmp_path`に固定するフィクスチャ。

    各テストファイルで`@pytest.fixture(autouse=True)`として再エクスポートするか、
    conftest から直接参照することで利用できる。
    """
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def count_config_warnings(needle: str) -> int:
    """`source == "config"` かつメッセージに `needle` を含む警告の件数を返す。

    `needle` に空文字列を渡すと `source == "config"` の全件をカウントする。
    """
    return sum(1 for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config" and needle in w["message"])


def shared_prefix_length(a: str, b: str) -> int:
    """2つの文字列の共通プレフィックス長を返す。"""
    shared = 0
    for ca, cb in zip(a, b, strict=False):
        if ca != cb:
            break
        shared += 1
    return shared


def seed_archive_run(
    cache_root: pathlib.Path,
    *,
    commands: list[str] | None = None,
    files: int = 3,
    exit_code: int = 0,
    tool_results: list[tuple[str, int, str, list]] | None = None,
    resolution_failed_tools: set[str] | None = None,
) -> str:
    """テスト用の run をアーカイブに書き込み、`run_id` を返す。

    `tool_results` は `(tool, returncode, output, errors)` のタプル列。
    run_id は ULID で生成され、ミリ秒解像度のタイムスタンプ部分を含む。
    連続呼び出しで同一 ULID が返る衝突を避けるため、冒頭に 1 ミリ秒のスリープを挿入する。
    """
    time.sleep(0.001)
    store = pyfltr.state.archive.ArchiveStore(cache_root=cache_root)
    run_id = store.start_run(commands=commands or ["ruff-check"], files=files)
    for tool, returncode, output, errors in tool_results or []:
        result = make_command_result(
            tool,
            returncode=returncode,
            output=output,
            errors=errors,
            resolution_failed=tool in (resolution_failed_tools or set()),
        )
        store.write_tool_result(run_id, result)
    store.finalize_run(run_id, exit_code=exit_code, commands=commands, files=files)
    return run_id
