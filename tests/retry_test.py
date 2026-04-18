# pylint: disable=missing-module-docstring
# pylint: disable=missing-function-docstring

import pathlib

import pytest

import pyfltr.retry
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error

# --- build_retry_args_template ---


def test_build_retry_args_template_preserves_options():
    """--no-fix / --output-format などのフラグが retry_command テンプレートに残る。"""
    args = [
        "run",
        "--no-fix",
        "--output-format",
        "jsonl",
        "--commands=ruff-check",
        "src/foo.py",
    ]
    template = pyfltr.retry.build_retry_args_template(args)
    assert "--no-fix" in template
    assert "--output-format" in template
    # --commands=VALUE 形式は --commands= プレースホルダに置換される
    assert "--commands=" in template


def test_build_retry_args_template_removes_only_failed():
    """--only-failed フラグが retry_command テンプレートから除去される。"""
    args = ["run", "--only-failed", "--commands=ruff-check", "src/foo.py"]
    template = pyfltr.retry.build_retry_args_template(args)
    assert "--only-failed" not in template
    # 他のオプションは保持される
    assert "--commands" in " ".join(template)


def test_build_retry_args_template_removes_from_run_space_separated():
    """--from-run <value>（空白区切り）が retry_command テンプレートから除去される。"""
    args = ["run", "--only-failed", "--from-run", "01ABCDEF", "--commands=ruff-check"]
    template = pyfltr.retry.build_retry_args_template(args)
    assert "--from-run" not in template
    assert "01ABCDEF" not in template
    # --only-failed も除去される
    assert "--only-failed" not in template


def test_build_retry_args_template_removes_from_run_equals_separated():
    """--from-run=<value>（等号区切り）が retry_command テンプレートから除去される。"""
    args = ["run", "--only-failed", "--from-run=01ABCDEF", "--commands=ruff-check"]
    template = pyfltr.retry.build_retry_args_template(args)
    assert "--from-run" not in " ".join(template)
    assert "01ABCDEF" not in template
    # --only-failed も除去される
    assert "--only-failed" not in template


def test_build_retry_args_template_only_failed_not_in_retry_command(tmp_path):
    """生成された retry_command に --only-failed が含まれない。"""
    args = ["run", "--only-failed", "--commands=ruff-check", "src/foo.py"]
    template = pyfltr.retry.build_retry_args_template(args)
    retry = pyfltr.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="ruff-check",
        target_files=[],
        original_cwd=str(tmp_path),
    )
    assert "--only-failed" not in retry


def test_build_retry_args_template_from_run_not_in_retry_command(tmp_path):
    """生成された retry_command に --from-run が含まれない。"""
    args = ["run", "--only-failed", "--from-run", "01ABCDEF", "--commands=ruff-check"]
    template = pyfltr.retry.build_retry_args_template(args)
    retry = pyfltr.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="ruff-check",
        target_files=[],
        original_cwd=str(tmp_path),
    )
    assert "--from-run" not in retry
    assert "01ABCDEF" not in retry


# --- build_retry_command ---


def test_build_retry_command_replaces_commands_and_targets(tmp_path):
    """retry_command が --commands と末尾ターゲットを差し替える。"""
    template = pyfltr.retry.build_retry_args_template(["run", "--no-fix", "--commands", "ruff-check", "src/foo.py"])
    retry = pyfltr.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="mypy",
        target_files=[pathlib.Path("pkg/bar.py")],
        original_cwd=str(tmp_path),
    )
    # --commands は置換される
    assert " mypy " in f" {retry} "
    # --no-fix は保持
    assert "--no-fix" in retry
    # ターゲットは original_cwd 基準の絶対パス
    assert str(tmp_path) in retry


def test_build_retry_command_missing_commands_inserts(tmp_path):
    """--commands 未指定時は自動で追記される。"""
    template = pyfltr.retry.build_retry_args_template(["run", "src/"])
    retry = pyfltr.retry.build_retry_command(
        template,
        ["pyfltr"],
        tool="mypy",
        target_files=[],
        original_cwd=str(tmp_path),
    )
    assert "--commands" in retry
    assert "mypy" in retry


# --- filter_failed_files ---


def test_filter_failed_files_empty_errors_returns_empty():
    """errors が空なら空リストを返す (絞り込み対象なし)。"""
    result = _make_result(
        "mypy",
        returncode=0,
        target_files=[pathlib.Path("src/a.py"), pathlib.Path("src/b.py")],
    )
    assert not pyfltr.retry.filter_failed_files(result)


def test_filter_failed_files_intersects_target_files():
    """errors.file と target_files の交差を target_files の並び順で返す。"""
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
    filtered = pyfltr.retry.filter_failed_files(result)
    # target_files の並び順 (a.py, c.py) を維持する
    assert filtered == [pathlib.Path("src/a.py"), pathlib.Path("src/c.py")]


def test_filter_failed_files_outside_target_files_returns_empty():
    """errors.file が target_files に含まれなければ空。"""
    errors = [_make_error("mypy", "src/other.py", 1, "err")]
    result = _make_result(
        "mypy",
        returncode=1,
        errors=errors,
        target_files=[pathlib.Path("src/a.py")],
    )
    assert not pyfltr.retry.filter_failed_files(result)


# --- populate_retry_command ---


def test_populate_retry_command_uses_filtered_files(tmp_path):
    """絞り込み後の target_files のみが retry_command に反映される。"""
    errors = [_make_error("mypy", "src/b.py", 1, "err")]
    result = _make_result(
        "mypy",
        returncode=1,
        errors=errors,
        target_files=[pathlib.Path("src/a.py"), pathlib.Path("src/b.py")],
    )
    template = pyfltr.retry.build_retry_args_template(["run", "--commands", "mypy"])
    pyfltr.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        original_cwd=str(tmp_path),
    )
    assert result.retry_command is not None
    assert "b.py" in result.retry_command
    assert "a.py" not in result.retry_command


def test_populate_retry_command_skips_for_cached(tmp_path):
    """cached=True の CommandResult では retry_command を埋めない。"""
    result = _make_result("mypy", returncode=0, cached=True, cached_from="01ABC")
    template = pyfltr.retry.build_retry_args_template(["run", "--commands", "mypy"])
    pyfltr.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        original_cwd=str(tmp_path),
    )
    assert result.retry_command is None


def test_populate_retry_command_skips_for_success(tmp_path):
    """成功 (has_error=False) の CommandResult では retry_command を埋めない。"""
    result = _make_result("mypy", returncode=0)
    template = pyfltr.retry.build_retry_args_template(["run", "--commands", "mypy"])
    pyfltr.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        original_cwd=str(tmp_path),
    )
    assert result.retry_command is None


def test_populate_retry_command_skips_for_formatted(tmp_path):
    """formatter によるファイル修正のみ (returncode!=0 だが has_error=False) では埋めない。"""
    # 例: ruff-format が差分を検出し returncode=1 を返すが、書き込み済みのため has_error=False
    result = _make_result("ruff-format", returncode=1, has_error=False, command_type="formatter")
    template = pyfltr.retry.build_retry_args_template(["run", "--commands", "ruff-format"])
    pyfltr.retry.populate_retry_command(
        result,
        retry_args_template=template,
        launcher_prefix=["pyfltr"],
        original_cwd=str(tmp_path),
    )
    assert result.retry_command is None


# --- detect_launcher_prefix ---


def test_detect_launcher_prefix_returns_list():
    """detect_launcher_prefix が少なくとも 1 要素以上のリストを返す。"""
    prefix = pyfltr.retry.detect_launcher_prefix()
    assert isinstance(prefix, list)
    assert len(prefix) >= 1


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
    """--only-failed / --from-run の各形式がテンプレートから除去される（パラメーター化）。"""
    template = pyfltr.retry.build_retry_args_template(args)
    joined = " ".join(template)
    for token in expected_absent:
        assert token not in joined
