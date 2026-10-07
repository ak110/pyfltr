import pathlib

import pytest

import pyfltr.cli.main
import pyfltr.cli.output_format
import pyfltr.command.only_failed
import pyfltr.output.logging_
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.only_failed
import pyfltr.warnings_

# ---------------------------------------------------------------------------
# ToolTargets dataclass
# ---------------------------------------------------------------------------


def test_tool_targets_fallback_default() -> None:
    """fallback_default()はmode="fallback"、files=()で生成される。"""
    t = pyfltr.command.only_failed.ToolTargets.fallback_default()
    assert t.mode == "fallback"
    assert len(t.files) == 0


def test_tool_targets_with_files(tmp_path: pathlib.Path) -> None:
    """with_files()はmode="files"、指定ファイルをtupleで保持する。"""
    f = tmp_path / "a.py"
    t = pyfltr.command.only_failed.ToolTargets.with_files([f])
    assert t.mode == "files"
    assert t.files == (f,)


def test_tool_targets_resolve_files_fallback_returns_all_files(tmp_path: pathlib.Path) -> None:
    """fallbackモードはall_filesをそのまま返す。"""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    t = pyfltr.command.only_failed.ToolTargets.fallback_default()
    assert t.resolve_files([a, b]) == [a, b]


def test_tool_targets_resolve_files_files_mode(tmp_path: pathlib.Path) -> None:
    """filesモードはall_filesとの交差をall_filesの順序で返し、交差空では空リストを返す。"""
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    c = tmp_path / "c.py"
    d = tmp_path / "d.py"
    t = pyfltr.command.only_failed.ToolTargets.with_files([a, c, b])
    assert t.resolve_files([b, a]) == [b, a]
    assert not t.resolve_files([d])


def test_tool_targets_is_frozen() -> None:
    """frozen=Trueなのでフィールドへの代入はTypeErrorになる。"""
    t = pyfltr.command.only_failed.ToolTargets.fallback_default()
    with pytest.raises((TypeError, AttributeError)):
        # frozen=Trueへの代入が実行時にTypeErrorを送出することを検証するため、型チェッカーの警告を抑止する。
        t.mode = "files"  # type: ignore[misc]  # ty: ignore[invalid-assignment]
