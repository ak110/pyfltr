import pathlib

import pytest

import pyfltr.grep_.matcher


def test_compile_pattern_basic_regex() -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo.bar"],
        fixed_strings=False,
        ignore_case=False,
        smart_case=False,
        word_regexp=False,
        line_regexp=False,
        multiline=False,
    )
    assert pattern.search("foo.bar") is not None
    assert pattern.search("fooXbar") is not None


def test_compile_pattern_fixed_strings() -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo.bar"],
        fixed_strings=True,
        ignore_case=False,
        smart_case=False,
        word_regexp=False,
        line_regexp=False,
        multiline=False,
    )
    assert pattern.search("foo.bar") is not None
    assert pattern.search("fooXbar") is None


def test_compile_pattern_ignore_case() -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo"],
        fixed_strings=False,
        ignore_case=True,
        smart_case=False,
        word_regexp=False,
        line_regexp=False,
        multiline=False,
    )
    assert pattern.search("FOO") is not None
    assert pattern.search("Foo") is not None


@pytest.mark.parametrize(
    "needle,target,should_match",
    [
        ("foo", "FOO", True),
        ("Foo", "foo", False),
        ("Foo", "Foo", True),
    ],
)
def test_compile_pattern_smart_case(needle: str, target: str, should_match: bool) -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        [needle],
        fixed_strings=False,
        ignore_case=False,
        smart_case=True,
        word_regexp=False,
        line_regexp=False,
        multiline=False,
    )
    matched = pattern.search(target) is not None
    assert matched is should_match


def test_compile_pattern_word_regexp() -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo"],
        fixed_strings=False,
        ignore_case=False,
        smart_case=False,
        word_regexp=True,
        line_regexp=False,
        multiline=False,
    )
    assert pattern.search("foo bar") is not None
    assert pattern.search("foobar") is None


def test_compile_pattern_line_regexp() -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo"],
        fixed_strings=False,
        ignore_case=False,
        smart_case=False,
        word_regexp=False,
        line_regexp=True,
        multiline=False,
    )
    text = "foo\nfoobar\nfoo\n"
    matches = list(pattern.finditer(text))
    assert len(matches) == 2
    assert all(m.group(0) == "foo" for m in matches)


def test_compile_pattern_multiline() -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo.+bar"],
        fixed_strings=False,
        ignore_case=False,
        smart_case=False,
        word_regexp=False,
        line_regexp=False,
        multiline=True,
    )
    assert pattern.search("foo\nbar") is not None


def test_compile_pattern_multiple_patterns() -> None:
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo", "bar"],
        fixed_strings=False,
        ignore_case=False,
        smart_case=False,
        word_regexp=False,
        line_regexp=False,
        multiline=False,
    )
    assert pattern.search("foo") is not None
    assert pattern.search("bar") is not None
    assert pattern.search("baz") is None


def test_compile_pattern_invalid_regex_raises_value_error() -> None:
    with pytest.raises(ValueError):
        pyfltr.grep_.matcher.compile_pattern(
            ["foo("],
            fixed_strings=False,
            ignore_case=False,
            smart_case=False,
            word_regexp=False,
            line_regexp=False,
            multiline=False,
        )


def test_compile_pattern_empty_patterns_raises_value_error() -> None:
    with pytest.raises(ValueError):
        pyfltr.grep_.matcher.compile_pattern(
            [],
            fixed_strings=False,
            ignore_case=False,
            smart_case=False,
            word_regexp=False,
            line_regexp=False,
            multiline=False,
        )


def test_read_pattern_file_skips_empty_lines(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "patterns.txt"
    target.write_text("foo\n\nbar\n\nbaz\n", encoding="utf-8")

    result = pyfltr.grep_.matcher.read_pattern_file(target)

    assert result == ["foo", "bar", "baz"]


def test_read_pattern_file_handles_crlf(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "patterns.txt"
    target.write_text("foo\r\nbar\r\nbaz", encoding="utf-8")

    result = pyfltr.grep_.matcher.read_pattern_file(target)

    assert result == ["foo", "bar", "baz"]
