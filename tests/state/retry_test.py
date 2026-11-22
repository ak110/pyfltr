import io
import pathlib
import sys

import pytest

import pyfltr.state.retry
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


def test_build_retry_args_template_preserves_options():
    args = [
        "run",
        "--no-fix",
        "--output-format",
        "jsonl",
        "--commands=ruff-check",
        "src/foo.py",
    ]
    template = pyfltr.state.retry.build_retry_args_template(args)
    assert "--no-fix" in template
    assert "--output-format" in template
    assert "--commands=" in template


def test_build_retry_args_template_removes_only_failed():
    args = ["run", "--only-failed", "--commands=ruff-check", "src/foo.py"]
    template = pyfltr.state.retry.build_retry_args_template(args)
    assert "--only-failed" not in template
    assert "--commands" in " ".join(template)


def test_build_retry_args_template_removes_from_run_space_separated():
    args = ["run", "--only-failed", "--from-run", "01ABCDEF", "--commands=ruff-check"]
    template = pyfltr.state.retry.build_retry_args_template(args)
    assert "--from-run" not in template
    assert "01ABCDEF" not in template
    assert "--only-failed" not in template


def test_build_retry_args_template_removes_from_run_equals_separated():
    args = ["run", "--only-failed", "--from-run=01ABCDEF", "--commands=ruff-check"]
    template = pyfltr.state.retry.build_retry_args_template(args)
    assert "--from-run" not in " ".join(template)
    assert "01ABCDEF" not in template
    assert "--only-failed" not in template


def test_build_retry_args_template_only_failed_not_in_retry_command(tmp_path):
    args = ["run", "--only-failed", "--commands=ruff-check", "src/foo.py"]
    template = pyfltr.state.retry.build_retry_args_template(args)
    retry = pyfltr.state.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="ruff-check",
        target_files=[],
        target_base_cwd=str(tmp_path),
    )
    assert "--only-failed" not in retry


def test_build_retry_args_template_from_run_not_in_retry_command(tmp_path):
    args = ["run", "--only-failed", "--from-run", "01ABCDEF", "--commands=ruff-check"]
    template = pyfltr.state.retry.build_retry_args_template(args)
    retry = pyfltr.state.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="ruff-check",
        target_files=[],
        target_base_cwd=str(tmp_path),
    )
    assert "--from-run" not in retry
    assert "01ABCDEF" not in retry


def test_build_retry_command_replaces_commands_and_targets(tmp_path):
    template = pyfltr.state.retry.build_retry_args_template(["run", "--no-fix", "--commands", "ruff-check", "src/foo.py"])
    retry = pyfltr.state.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="mypy",
        target_files=[pathlib.Path("pkg/bar.py")],
        target_base_cwd=str(tmp_path),
    )
    assert " mypy " in f" {retry} "
    assert "--no-fix" in retry
    assert str(tmp_path) in retry


def test_build_retry_command_missing_commands_inserts(tmp_path):
    template = pyfltr.state.retry.build_retry_args_template(["run", "src/"])
    retry = pyfltr.state.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="mypy",
        target_files=[],
        target_base_cwd=str(tmp_path),
    )
    assert "--commands" in retry
    assert "mypy" in retry


def test_filter_failed_files_empty_errors_returns_empty():
    result = _make_result(
        "mypy",
        returncode=0,
        target_files=[pathlib.Path("src/a.py"), pathlib.Path("src/b.py")],
    )
    assert not pyfltr.state.retry.filter_failed_files(result, start_cwd=pathlib.Path.cwd())


def test_filter_failed_files_intersects_target_files():
    errors = [
        _make_error("mypy", "src/c.py", 1, "err"),
        _make_error("mypy", "src/a.py", 2, "err"),
    ]
    result = _make_result(
        "mypy",
        returncode=1,
        errors=errors,
        target_files=[
            pathlib.Path("src/a.py"),
            pathlib.Path("src/b.py"),
            pathlib.Path("src/c.py"),
        ],
    )
    filtered = pyfltr.state.retry.filter_failed_files(result, start_cwd=pathlib.Path.cwd())
    assert filtered == [pathlib.Path("src/a.py"), pathlib.Path("src/c.py")]


def test_filter_failed_files_outside_target_files_returns_empty():
    errors = [_make_error("mypy", "src/other.py", 1, "err")]
    result = _make_result(
        "mypy",
        returncode=1,
        errors=errors,
        target_files=[pathlib.Path("src/a.py")],
    )
    assert not pyfltr.state.retry.filter_failed_files(result, start_cwd=pathlib.Path.cwd())


def test_filter_failed_files_matches_targets_with_dot_dot_and_outside_relative(tmp_path: pathlib.Path) -> None:
    start = tmp_path / "proj"
    outside = (tmp_path / "outside" / "out.md").as_posix()
    errors = [
        _make_error("markdownlint", "a.md", 1, "err"),
        _make_error("markdownlint", outside, 1, "err"),
    ]
    targets = [pathlib.Path("sub/../a.md"), pathlib.Path("../outside/out.md")]
    result = _make_result("markdownlint", returncode=1, errors=errors, target_files=targets)
    assert pyfltr.state.retry.filter_failed_files(result, start_cwd=start) == targets


def test_populate_retry_command_uses_filtered_files(tmp_path):
    errors = [_make_error("mypy", "src/b.py", 1, "err")]
    result = _make_result(
        "mypy",
        returncode=1,
        errors=errors,
        target_files=[pathlib.Path("src/a.py"), pathlib.Path("src/b.py")],
    )
    template = pyfltr.state.retry.build_retry_args_template(["run", "--commands", "mypy"])
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        target_base_cwd=str(tmp_path),
    )
    assert result.retry_command is not None
    assert "b.py" in result.retry_command
    assert "a.py" not in result.retry_command


@pytest.mark.parametrize("cached", [True, False])
def test_populate_retry_command_skips_for_cached(tmp_path, cached):
    result = _make_result("mypy", returncode=1, cached=cached, cached_from="01ABC" if cached else None)
    template = pyfltr.state.retry.build_retry_args_template(["run", "--commands", "mypy"])
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        target_base_cwd=str(tmp_path),
    )
    assert (result.retry_command is None) is cached


def test_populate_retry_command_skips_for_success(tmp_path):
    result = _make_result("mypy", returncode=0)
    template = pyfltr.state.retry.build_retry_args_template(["run", "--commands", "mypy"])
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        target_base_cwd=str(tmp_path),
    )
    assert result.retry_command is None


def test_populate_retry_command_skips_for_formatted(tmp_path):
    result = _make_result("ruff-format", returncode=1, formatter_failed=False, command_type="formatter")
    template = pyfltr.state.retry.build_retry_args_template(["run", "--commands", "ruff-format"])
    pyfltr.state.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        target_base_cwd=str(tmp_path),
    )
    assert result.retry_command is None


@pytest.mark.parametrize(
    ("parent_command", "expected"),
    [
        (b"uv\0run\0pyfltr\0", ["uv", "run", "pyfltr"]),
        (b"uvx\0pyfltr\0", ["uvx", "pyfltr"]),
        (None, ["pyfltr"]),
    ],
)
def test_detect_launcher_prefix(monkeypatch: pytest.MonkeyPatch, parent_command: bytes | None, expected: list[str]) -> None:
    def read_proc(path: str, mode: str = "r", **_kwargs: str) -> io.StringIO | io.BytesIO:
        if path == "/proc/self/status" and mode == "r":
            return io.StringIO("PPid:\t42\n")
        if path == "/proc/42/cmdline" and mode == "rb" and parent_command is not None:
            return io.BytesIO(parent_command)
        raise FileNotFoundError(path)

    monkeypatch.setattr(pyfltr.state.retry, "open", read_proc, raising=False)
    monkeypatch.setattr(sys, "argv", ["pyfltr"])
    assert pyfltr.state.retry.detect_launcher_prefix() == expected


@pytest.mark.parametrize(
    ("args", "expected_absent"),
    [
        (
            ["run", "--only-failed", "--from-run", "abc123", "--no-fix"],
            ["--only-failed", "--from-run", "abc123"],
        ),
        (
            ["run", "--only-failed", "--from-run=abc123", "--no-fix"],
            ["--only-failed", "--from-run", "abc123"],
        ),
        (
            ["ci", "--only-failed"],
            ["--only-failed"],
        ),
    ],
)
def test_build_retry_args_template_removes_archive_flags(args, expected_absent):
    template = pyfltr.state.retry.build_retry_args_template(args)
    joined = " ".join(template)
    for token in expected_absent:
        assert token not in joined
