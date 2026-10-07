import pyfltr.diagnostics
import pyfltr.output.diagnostics
import pyfltr.parsing.entry
import pyfltr.parsing.pytest
import pyfltr.tools


def test_parse_errors_custom_pattern() -> None:
    """カスタムerror-patternのテスト。"""
    pattern = r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)"
    output = "src/foo.py:10:5: some error\nsrc/bar.py:20:3: another error"
    errors = pyfltr.parsing.entry.parse_errors("custom-tool", output, error_pattern=pattern)
    assert len(errors) == 2
    assert errors[0].file == "src/foo.py"
    assert errors[0].line == 10
    assert errors[0].col == 5
    assert errors[0].message == "some error"
    assert errors[1].file == "src/bar.py"


def test_parse_errors_custom_pattern_normalizes_end_position() -> None:
    """利用者定義のerror-pattern経由でも終了位置を包含行へ補正する。"""
    pattern = r"(?P<file>[^:]+):(?P<line>\d+):(?P<col>\d+)-(?P<end_line>\d+):(?P<end_col>\d+):\s*(?P<message>.+)"
    output = "src/a.py:5:2-6:1: next line head\nsrc/b.py:5:2-5:9: in line"

    errors = pyfltr.parsing.entry.parse_errors("custom-tool", output, error_pattern=pattern)

    assert len(errors) == 2
    assert errors[0].end_line == 5
    assert errors[0].end_col is None
    assert errors[1].end_line == 5
    assert errors[1].end_col == 9


def test_extract_last_line_skips_separators() -> None:
    """区切り線のみの行をスキップして意味のある行を返す。"""
    output = "Some useful info\n===========================\n"
    result = pyfltr.parsing.entry.parse_summary("unknown-tool", output)
    assert result == "Some useful info"
