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


@pytest.mark.parametrize("command", ["semgrep", "sqlfluff"])
@pytest.mark.parametrize("runner", ["uv", "uvx"])
def test_execute_command_isolated_tool_missing_emits_uvx_direct_hint(
    command: str,
    runner: str,
) -> None:
    """分離ツールのuv/uvx起動失敗ではuvx直接実行とdirect切替だけを案内する。"""
    result = pyfltr.command.core_.CommandResult(
        command=command,
        command_type="linter",
        commandline=[runner, command],
        returncode=2,
        files=1,
        output=f"error: project 'sample' does not have '{command}' as a dependency\n",
        elapsed=0.1,
        effective_runner=runner,
    )
    pyfltr.command.tool_resolution.maybe_emit_uv_missing_tool_warning(result)

    warnings = pyfltr.warnings_.collected_warnings()
    uv_warnings = [
        warning
        for warning in warnings
        if warning["source"] == "tool-resolve" and "経路でのツール起動に失敗" in warning["message"]
    ]
    assert len(uv_warnings) == 1
    hint = uv_warnings[0].get("hint", "")
    assert f"uvx {command}" in hint
    assert f'{command}-runner = "direct"' in hint
    assert "pyfltr[python]" not in hint
    assert "uv add --dev" not in hint
