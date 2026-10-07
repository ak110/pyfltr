"""runnerとdispatcherが使うuv実行環境の共通フィクスチャ。"""

import typing

import pytest

import pyfltr.command.core_
import pyfltr.command.dispatcher
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.mise
import pyfltr.command.only_failed
import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_resolution
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.cache
import pyfltr.state.only_failed
import pyfltr.tools
import pyfltr.warnings_

# --- uv runner テスト ---


@pytest.fixture(name="setup_uv_runner")
def setup_uv_runner(monkeypatch: pytest.MonkeyPatch) -> typing.Callable[..., None]:
    """uv runner経路のテスト用ヘルパー。

    呼び出し: `setup_uv_runner(uv_lock=..., uv_available=..., python_bin_dir=...)`。
    `cwd_has_uv_lock` / `ensure_uv_available` をテスト引数で差し替える。
    `python_bin_dir` 指定時は `pyfltr.command.runner.shutil.which` も差し替えて、
    direct フォールバック後に解決される python ツールのパスを `<dir>/<name>` で返す。

    `cwd_has_uv_lock` / `ensure_uv_available` / `ensure_uvx_available` は
    `@functools.lru_cache(maxsize=1)` 付きでプロセス内固定化されるため、
    `monkeypatch.setattr` で関数自体を置換する形で差し替える（`lru_cache` 内部状態の
    クリアは行わず、関数オブジェクト差し替え経由で常に新しい結果を返させる）。
    `shutil.which` のmockはモジュールパス指定 `"pyfltr.command.runner.shutil.which"`
    を使う必要がある（モジュール内で `shutil` を別経路でimportしている場合に備えるため）。
    """

    def _apply(*, uv_lock: bool, uv_available: bool, python_bin_dir: str | None = None) -> None:
        monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda *_args: uv_lock)
        monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: uv_available)
        if python_bin_dir is not None:
            monkeypatch.setattr(
                "pyfltr.command.runner.shutil.which",
                lambda name: f"{python_bin_dir}/{name}" if name in pyfltr.command.runner.PYTHON_TOOL_BIN.values() else None,
            )

    return _apply
