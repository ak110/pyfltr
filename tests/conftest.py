"""pytest 共通定義。

`CommandResult`や`ErrorLocation`のダミーを生成するヘルパーと、
実行アーカイブのテストデータ生成ヘルパーを集約する。
各テストファイルで同じようなビルダーを書き散らかすとpylintの
duplicate-code（R0801）に掛かるため、ここに集約する。conftest.pyに置くのは
pre-commitのname-tests-testフックから除外されるため。
"""

import argparse
import pathlib

import pytest

import pyfltr.command
import pyfltr.config.config
import pyfltr.error_parser
import pyfltr.state.archive
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.warnings_


@pytest.fixture(autouse=True)
def _clear_warnings_between_tests() -> None:
    """全テストで警告状態を持ち越さないため、各テスト開始前に蓄積をクリアする。

    `pyfltr.warnings_`はモジュール変数としてプロセス内で共有されるため、
    テスト間のリークが発生すると順序依存や並列実行での非決定性を招く。
    conftest.pyでautouse化することで、各テストが空状態から始まることを保証する。
    """
    pyfltr.warnings_.clear()


@pytest.fixture(autouse=True)
def _isolate_output_format_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """全テストで出力形式を左右する環境変数を未設定状態に固定する。

    Claude Codeなどのエージェント環境では`AI_AGENT`が常時設定されるため、
    autouse未対応だと開発機の状態によって既定出力形式がjsonlへ揺らぐ。
    `PYFLTR_OUTPUT_FORMAT`も同様に開発者シェルで設定されている可能性があるため、
    両者を一律に未設定化する。個別テストは`monkeypatch.setenv`で必要時のみ上書きする。
    """
    monkeypatch.delenv("AI_AGENT", raising=False)
    monkeypatch.delenv("PYFLTR_OUTPUT_FORMAT", raising=False)


# `_get_mise_active_tools` 実装本体を保存しておく（モック上書き前の参照）。
# キャッシュキー検証系テストでは実装の挙動を直接確認したいため、モック前の関数オブジェクトを
# 経由して呼べるよう、本変数をテスト側から参照可能にする。
real_get_mise_active_tools = pyfltr.command._get_mise_active_tools  # pylint: disable=protected-access


@pytest.fixture(autouse=True)
def _default_mise_active_tools_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """全テストで `mise ls --current --json` 経路を「mise設定記述なし」相当へ固定する。

    `_get_mise_active_tools` は実環境のmise・cwd配下の `mise.toml` に依存して結果が変わるため、
    モック無しでは開発機の状態次第でテストが揺らぐ（tool spec省略形が混入する等）。
    既存テストはmise設定記述なし前提で従来形 `mise exec <tool>@latest -- <bin>` を期待しており、
    本フィクスチャでautouseに空のステータス`ok`結果を返すよう固定することでテスト全体の前提を揃える。
    新仕様（mise設定記述あり時のtool spec省略）を検証するテストは個別に上書きする。
    """
    monkeypatch.setattr("pyfltr.command._MISE_ACTIVE_TOOLS_CACHE", {}, raising=True)
    monkeypatch.setattr(
        "pyfltr.command._get_mise_active_tools",
        lambda config, *, allow_side_effects=False: pyfltr.command.MiseActiveToolsResult(status="ok"),
    )


@pytest.fixture(autouse=True)
def _isolate_global_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> pathlib.Path:
    """全テストで`PYFLTR_GLOBAL_CONFIG`をtmpパス配下のダミーパスへ固定する。

    開発機の`~/.config/pyfltr/config.toml`がテスト結果に影響することを防ぐ。
    各テストは`monkeypatch.setenv`で個別に上書き可能。
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
) -> pyfltr.command.ExecutionContext:
    """テスト用の ExecutionContext を生成する。

    `execute_command`を直接呼び出すテストで使用する。
    CLI/TUIフック系（on_output / is_interrupted / on_subprocess_start / on_subprocess_end）は
    テストでは不要なため省略（デフォルトのNoneが使われる）。
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
    resolution_failed: bool = False,
) -> pyfltr.command.CommandResult:
    """テスト用の CommandResult を生成する。

    `has_error`を省略した場合、`returncode`が0/None以外ならTrueに推定する。
    `errors`は`ErrorLocation`のリスト（省略時は空）。`target_files`は
    `retry_command`絞り込み（A案）のテスト用（省略時は空）。
    `archived`はテスト既定でTrue（smart truncationが適用される側）。
    実運用でのデフォルト（`CommandResult()`生成時のFalse）とは異なる点に注意。
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
        resolution_failed=resolution_failed,
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


def make_formatted_result(command: str = "ruff-format") -> pyfltr.command.CommandResult:
    """`status == "formatted"` になる最小の CommandResult を生成する。

    `main_test`など複数のテストファイルで同様の構築が必要なためconftest.pyに集約する。
    """
    return pyfltr.command.CommandResult(
        command, "formatter", [command], returncode=1, has_error=False, files=1, output="", elapsed=0.01
    )


def make_succeeded_result(command: str = "ruff-check") -> pyfltr.command.CommandResult:
    """`status == "succeeded"` になる最小の CommandResult を生成する。

    `main_test`など複数のテストファイルで同様の構築が必要なためconftest.pyに集約する。
    """
    return pyfltr.command.CommandResult(
        command, "linter", [command], returncode=0, has_error=False, files=1, output="", elapsed=0.01
    )


def make_archive_store(tmp_path: pathlib.Path) -> pyfltr.state.archive.ArchiveStore:
    """テスト用の`ArchiveStore`を生成する。

    `archive_test`などで`tmp_path`配下に隔離したstoreを作るための共通ファクトリー。
    """
    return pyfltr.state.archive.ArchiveStore(cache_root=tmp_path)


def make_args(*, no_exclude: bool = False) -> argparse.Namespace:
    """`execute_command`に渡す`argparse.Namespace`を生成する。

    `command_test`系のテストで`_make_args()`として重複定義されていたものを集約した。
    """
    return argparse.Namespace(shuffle=False, verbose=False, no_exclude=no_exclude)


@pytest.fixture
def _isolated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    """全テストで`PYFLTR_CACHE_DIR`を`tmp_path`に固定する。

    `runs_test` / `mcp_test`でautouse=Trueの同名fixtureとして重複定義されていたものを集約した。
    各テストファイルで`@pytest.fixture(autouse=True)`として再エクスポートするか、
    またはconftestから直接参照することで利用できる。
    """
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def count_config_warnings(needle: str) -> int:
    """`source=="config"`かつメッセージに`needle`を含む警告の件数を返す。

    `needle`に空文字列を渡すと`source=="config"`の全件をカウントする。
    """
    return sum(1 for w in pyfltr.warnings_.collected_warnings() if w["source"] == "config" and needle in w["message"])


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

    `tool_results`は`(tool, returncode, output, errors)`のタプル列。
    `runs_test` / `mcp_test`等で同じセットアップ手順を踏むため、
    duplicate-code（R0801）回避用にconftest.py側へ集約している。
    """
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
