import pathlib

import pyfltr.cli.main
import pyfltr.cli.output_format
import pyfltr.command.only_failed
import pyfltr.output.logging_
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.only_failed
import pyfltr.warnings_


def test_tool_targets_resolve_files_fallback_returns_all_files(tmp_path: pathlib.Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    t = pyfltr.command.only_failed.ToolTargets.fallback_default()
    assert t.resolve_files([a, b]) == [a, b]


def test_tool_targets_resolve_files_files_mode(tmp_path: pathlib.Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    d = tmp_path / "d.py"
    t = pyfltr.command.only_failed.ToolTargets.with_files([a, c, b])
    assert t.resolve_files([b, a]) == [b, a]
    assert not t.resolve_files([d])
