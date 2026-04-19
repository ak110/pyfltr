"""github_annotationsのテストコード。"""

import pyfltr.config
import pyfltr.github_annotations
from tests.conftest import make_command_result as _make_result
from tests.conftest import make_error_location as _make_error


def test_build_github_annotation_lines_severity_mapping() -> None:
    """severity 3 値が `::error` / `::warning` / `::notice` にマップされる。"""
    errors = [
        _make_error("tool", "a.py", 1, "e msg"),
        _make_error("tool", "a.py", 2, "w msg"),
        _make_error("tool", "a.py", 3, "i msg"),
    ]
    errors[0].severity = "error"
    errors[1].severity = "warning"
    errors[2].severity = "info"
    result = _make_result("tool", returncode=1, errors=errors)
    config = pyfltr.config.create_default_config()
    lines = pyfltr.github_annotations.build_github_annotation_lines([result], config)
    # 1 診断につき plain 行 + workflow command 行の 2 行が出る。
    assert len(lines) == 6
    assert lines[0].startswith("a.py:1: error: ")
    assert lines[1].startswith("::error ")
    assert lines[2].startswith("a.py:2: warning: ")
    assert lines[3].startswith("::warning ")
    assert lines[4].startswith("a.py:3: notice: ")
    assert lines[5].startswith("::notice ")


def test_build_github_annotation_lines_file_line_col() -> None:
    """file/line/col/title が workflow command 行に、tool/rule/msg が plain 行に含まれる。"""
    errors = [_make_error("ruff-check", "src/foo.py", 10, "unused", col=5)]
    errors[0].severity = "error"
    errors[0].rule = "F401"
    result = _make_result("ruff-check", returncode=1, errors=errors)
    config = pyfltr.config.create_default_config()
    lines = pyfltr.github_annotations.build_github_annotation_lines([result], config)
    plain, workflow = lines[0], lines[1]
    assert plain == "src/foo.py:10:5: error: [ruff-check: F401] unused"
    assert "file=src/foo.py" in workflow
    assert "line=10" in workflow
    assert "col=5" in workflow
    assert "title=ruff-check%3A F401" in workflow
    assert workflow.endswith("::unused")


def test_build_github_annotation_lines_message_escaping() -> None:
    """workflow command 行はメッセージの ``%`` / 改行をエンコードし、plain 行は改行を空白へ畳む。"""
    errors = [_make_error("tool", "a.py", 1, "100%\nline2")]
    errors[0].severity = "warning"
    result = _make_result("tool", returncode=1, errors=errors)
    config = pyfltr.config.create_default_config()
    lines = pyfltr.github_annotations.build_github_annotation_lines([result], config)
    assert lines[0] == "a.py:1: warning: [tool] 100% line2"
    assert lines[1].endswith("::100%25%0Aline2")


def test_build_github_annotation_lines_no_severity_fallback_warning() -> None:
    """severity 未設定は ``::warning`` にフォールバックする。"""
    errors = [_make_error("tool", "a.py", 1, "x")]
    result = _make_result("tool", returncode=1, errors=errors)
    config = pyfltr.config.create_default_config()
    lines = pyfltr.github_annotations.build_github_annotation_lines([result], config)
    assert lines[0].startswith("a.py:1: warning: ")
    assert lines[1].startswith("::warning ")


def test_build_github_annotation_lines_empty_when_no_errors() -> None:
    """diagnostic が無いツールは行を出さない。"""
    result = _make_result("mypy", returncode=0)
    config = pyfltr.config.create_default_config()
    lines = pyfltr.github_annotations.build_github_annotation_lines([result], config)
    assert not lines
