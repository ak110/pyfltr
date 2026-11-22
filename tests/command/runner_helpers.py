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


@pytest.fixture(name="setup_uv_runner")
def setup_uv_runner(monkeypatch: pytest.MonkeyPatch) -> typing.Callable[..., None]:

    def _apply(*, uv_lock: bool, uv_available: bool, python_bin_dir: str | None = None) -> None:
        monkeypatch.setattr(pyfltr.command.runner, "cwd_has_uv_lock", lambda *_args: uv_lock)
        monkeypatch.setattr(pyfltr.command.runner, "ensure_uv_available", lambda: uv_available)
        if python_bin_dir is not None:
            monkeypatch.setattr(
                "pyfltr.command.runner.shutil.which",
                lambda name: f"{python_bin_dir}/{name}" if name in pyfltr.command.runner.PYTHON_TOOL_BIN.values() else None,
            )

    return _apply
