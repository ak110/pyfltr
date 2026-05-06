"""grep_/matcher.py のテスト。"""

import pathlib

import pytest

import pyfltr.grep_.matcher


def test_compile_pattern_basic_regex() -> None:
    """単一の正規表現パターンが期待通りコンパイルされる。"""
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
    # 正規表現として`.`は任意1文字にマッチする
    assert pattern.search("fooXbar") is not None


def test_compile_pattern_fixed_strings() -> None:
    """fixed_strings=True で正規表現メタ文字がエスケープされる。"""
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
    # ドットはリテラルなのでXはマッチしない
    assert pattern.search("fooXbar") is None


def test_compile_pattern_ignore_case() -> None:
    """ignore_case=True で大文字小文字を区別しない。"""
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
        # smart_case ON + 大文字なし → 大文字小文字を無視
        ("foo", "FOO", True),
        # smart_case ON + 大文字あり → 厳密一致
        ("Foo", "foo", False),
        ("Foo", "Foo", True),
    ],
)
def test_compile_pattern_smart_case(needle: str, target: str, should_match: bool) -> None:
    """smart_case=True が大文字含有でignore_caseを切り替える。"""
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
    """word_regexp=True で語境界ありのみマッチする。"""
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
    # 連続する英数字内にあれば語境界が立たないためマッチしない
    assert pattern.search("foobar") is None


def test_compile_pattern_line_regexp() -> None:
    """line_regexp=True で行全体一致のみマッチする。"""
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
    assert len(matches) == 2  # 「foo」だけの行2行のみ
    # foobar 行はマッチしない
    assert all(m.group(0) == "foo" for m in matches)


def test_compile_pattern_multiline() -> None:
    """multiline=True で改行を跨いだマッチを検出できる。"""
    pattern = pyfltr.grep_.matcher.compile_pattern(
        ["foo.+bar"],
        fixed_strings=False,
        ignore_case=False,
        smart_case=False,
        word_regexp=False,
        line_regexp=False,
        multiline=True,
    )
    # multiline指定により`.`が改行も含むようになる（DOTALL）
    assert pattern.search("foo\nbar") is not None


def test_compile_pattern_multiple_patterns() -> None:
    """複数パターン指定はalternation結合される。"""
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
    """不正な正規表現はValueErrorになる。"""
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
    """パターン未指定はValueErrorになる。"""
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
    """空行が除外される。"""
    target = tmp_path / "patterns.txt"
    target.write_text("foo\n\nbar\n\nbaz\n", encoding="utf-8")

    result = pyfltr.grep_.matcher.read_pattern_file(target)

    assert result == ["foo", "bar", "baz"]


def test_read_pattern_file_handles_crlf(tmp_path: pathlib.Path) -> None:
    """CRLF混在の改行コードに対応する。"""
    target = tmp_path / "patterns.txt"
    target.write_text("foo\r\nbar\r\nbaz", encoding="utf-8")

    result = pyfltr.grep_.matcher.read_pattern_file(target)

    assert result == ["foo", "bar", "baz"]
