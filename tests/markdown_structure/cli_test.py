import pathlib
import subprocess
import sys

import jinja2
import markdown_it
import pytest

import pyfltr.paths


def _run(*args: str | pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pyfltr.markdown_structure", *(str(a) for a in args)],
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def _where(path: pathlib.Path) -> str:
    return pyfltr.paths.normalize_separators(str(path))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("甲。乙。\n", "甲。\n乙。\n"),
        ("- 甲。乙。丙。\n", "- 甲。\n  乙。\n  丙。\n"),
        ("12. 甲。乙。\n", "12. 甲。\n    乙。\n"),
        ("1. - 甲。乙。\n", "1. - 甲。\n     乙。\n"),
        ("- 親\n  - 甲。乙。\n", "- 親\n  - 甲。\n    乙。\n"),
        ("- 親\n\n  甲。乙。\n", "- 親\n\n  甲。\n  乙。\n"),
        ("**甲。**乙。\n", "**甲。**\n乙。\n"),
        ("甲。**乙。**\n", "甲。\n**乙。**\n"),
        ("[甲。](../doc.md#節)乙[^注]。\n", "[甲。](../doc.md#節)\n乙[^注]。\n"),
        ("甲。`乙`。\n", "甲。\n`乙`。\n"),
        ("\\`甲。乙。\\`\n", "\\`甲。\n乙。\n\\`\n"),
        ("\ufeff甲。乙。\r\n", "\ufeff甲。\r\n乙。\r\n"),
    ],
)
def test_fix_preserves_source_and_list_structure(tmp_path: pathlib.Path, source: str, expected: str) -> None:
    path = tmp_path / "doc.md"
    path.write_bytes(source.encode("utf-8"))
    assert _run(path).returncode == 1
    assert path.read_bytes() == source.encode("utf-8")
    assert _run("--fix", path).returncode == 0
    assert path.read_bytes() == expected.encode("utf-8")
    assert _run("--fix", path).returncode == 0
    assert path.read_bytes() == expected.encode("utf-8")


@pytest.mark.parametrize(
    "source",
    [
        "本文（補足。説明）。\n次。\n",
        "本文(補足。説明)。\n次。\n",
        "「甲。『乙。丙。』丁。」とする。\n",
        "「甲。\n乙。」とする。\n",
        "`甲。乙。（外（内））`とする。\n",
        "``甲。`乙。（外（内））``とする。\n",
        "`甲。\n乙。（外（内））`とする。\n",
        "```md\n甲。乙。（外（内））\n```\n",
        "    甲。乙。（外（内））\n",
        "<!-- 甲。乙。（外（内）） -->\n本文。\n",
        "---\nname: 甲。乙。（外（内））\n---\n本文。\n",
        "# 見出し。続き。\n",
        "| 見出し |\n| --- |\n| 甲。乙。 |\n",
        "> 甲。乙。（外（内））\n> 引用。続き。\n",
        "逐語引用: 甲。乙。\n",
        "甲。\n乙。\n",
        "Plain English sentence. Another one.\n",
    ],
)
def test_excluded_constructs_are_unchanged(tmp_path: pathlib.Path, source: str) -> None:
    path = tmp_path / "doc.md"
    path.write_text(source, encoding="utf-8")
    assert _run("--fix", path).returncode == 0
    assert path.read_text(encoding="utf-8") == source


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("文A。- 次の文は箇条になる。\n", "Markdownの構造が変わる"),
        ("文B。1. 番号付きになる。\n", "Markdownの構造が変わる"),
        ("文C。> 引用になる。\n", "Markdownの構造が変わる"),
        ("記録した。#567 を参照する。\n", "行頭の #"),
        ("甲。===\n", "Markdownの構造が変わる"),
        ("甲。<div>乙。</div>\n", "Markdownの構造が変わる"),
        ("甲。| a | b |\n| - | - |\n", "Markdownの構造が変わる"),
        ("{{ value }}。乙。\n", "Jinja2構文を含む"),
        ("{# 注記 #}甲。乙。\n", "Jinja2構文を含む"),
    ],
)
def test_fix_reports_unsafe_break_without_writing(tmp_path: pathlib.Path, source: str, reason: str) -> None:
    path = tmp_path / "doc.md"
    path.write_bytes(source.encode())
    result = _run("--fix", path)
    assert result.returncode == 1
    assert f"{_where(path)}:1:" in result.stdout
    assert "1文1行へ改行する" in result.stdout
    assert reason in result.stdout
    assert path.read_bytes() == source.encode()


@pytest.mark.parametrize("marker", ["{{", "{%", "{#"])
def test_fix_breaks_ordinary_text_with_unmatched_template_marker(tmp_path: pathlib.Path, marker: str) -> None:
    path = tmp_path / "doc.md"
    path.write_text(f"開始記号の例。\n\n```text\n{marker}\n```\n\n最初の文。次の文。\n", encoding="utf-8")
    result = _run("--fix", path)
    assert result.returncode == 0
    assert path.read_text(encoding="utf-8") == f"開始記号の例。\n\n```text\n{marker}\n```\n\n最初の文。\n次の文。\n"


def test_fix_keeps_safe_break_in_same_paragraph(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "doc.md"
    path.write_bytes("甲。乙。- 丙。丁。\n".encode())
    result = _run("--fix", path)
    assert result.returncode == 1
    assert f"{_where(path)}:2:2:" in result.stdout
    assert path.read_bytes() == "甲。\n乙。- 丙。\n丁。\n".encode()


def test_fix_does_not_combine_breaks_that_create_table(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "doc.md"
    path.write_bytes("甲。| a | b。| - | - |\n".encode())
    result = _run("--fix", path)
    assert result.returncode == 1
    assert "Markdownの構造が変わる" in result.stdout
    assert path.read_bytes() == "甲。| a | b。\n| - | - |\n".encode()


def test_fix_preserves_heading_joined_by_jinja2_whitespace_control(tmp_path: pathlib.Path) -> None:
    source = '{{ "# " -}}\n\n甲。乙。\n'
    path = tmp_path / "doc.md"
    path.write_bytes(source.encode())
    result = _run("--fix", path)
    assert result.returncode == 1
    assert f"{_where(path)}:3:2:" in result.stdout
    assert "Jinja2構文を含む" in result.stdout
    assert path.read_bytes() == source.encode()

    def blocks(template: str) -> list[str]:
        rendered = jinja2.Environment().from_string(template).render()
        return [token.type for token in markdown_it.MarkdownIt("commonmark").parse(rendered)]

    assert blocks(source) == ["heading_open", "inline", "heading_close"]
    assert blocks(path.read_bytes().decode()) == blocks(source)


def test_fix_preserves_rendered_jinja2_list(tmp_path: pathlib.Path) -> None:
    source = "{% for item in items %}- {{ item }}。説明。\n{% endfor %}\n"
    path = tmp_path / "doc.md"
    path.write_bytes(source.encode())
    result = _run("--fix", path)
    assert result.returncode == 1
    assert "Jinja2構文を含む" in result.stdout
    assert path.read_bytes() == source.encode()

    def structure(template: str) -> list[tuple[str, int, bool]]:
        rendered = jinja2.Environment().from_string(template).render(items=["甲", "乙", "丙"])
        tokens = markdown_it.MarkdownIt("commonmark").parse(rendered)
        return [(token.type, token.nesting, token.hidden) for token in tokens]

    before = structure(source)
    assert before.count(("list_item_open", 1, False)) == 3
    assert before.count(("paragraph_open", 1, True)) == 3
    assert structure(path.read_bytes().decode()) == before


@pytest.mark.parametrize(
    ("source", "location"),
    [
        ("本文（外（内））。\n", ":1:5:"),
        ("本文（外\n（内））。\n", ":2:1:"),
        ("| 見出し |\n| --- |\n| 本文（外（内）） |\n", ":3:7:"),
        ("# 本文（外（内））\n", ":1:7:"),
    ],
)
def test_nested_parentheses_are_errors_even_with_fix(tmp_path: pathlib.Path, source: str, location: str) -> None:
    path = tmp_path / "doc.md"
    path.write_text(source, encoding="utf-8")
    result = _run("--fix", path)
    assert result.returncode == 1
    assert f"{_where(path)}{location} error: 全角丸括弧の入れ子を解消する" in result.stdout
    assert path.read_text(encoding="utf-8") == source


@pytest.mark.parametrize(
    ("source", "locations"),
    [
        (
            "| 列1 | 列2 |\n| --- | --- |\n| 外（内（補足）） | 正常 |\n\n後続（外（内））。\n",
            [":3:6:", ":5:5:"],
        ),
        ("| 列1 | 列2 |\n| --- | --- |\n| 外（ | 続き |\n\n閉じる）。\n\n後続（正常）。\n", []),
        ("| コード | 本文 |\n| --- | --- |\n| `（外（内））` | 単独（補足） |\n\n後続（正常）。\n", []),
    ],
)
def test_table_positions_and_following_paragraph(tmp_path: pathlib.Path, source: str, locations: list[str]) -> None:
    path = tmp_path / "table.md"
    path.write_text(source, encoding="utf-8")
    result = _run(path)
    assert result.returncode == int(bool(locations))
    assert result.stdout.splitlines() == [
        f"{_where(path)}{location} error: 全角丸括弧の入れ子を解消する" for location in locations
    ]
    assert path.read_text(encoding="utf-8") == source


def test_fixtures_directory_is_not_excluded(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "fixtures" / "request.md"
    path.parent.mkdir()
    path.write_text("甲。乙。\n", encoding="utf-8")
    assert _run(path).returncode == 1


def test_input_error_takes_precedence_and_other_files_are_checked(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff")
    violation = tmp_path / "doc.md"
    violation.write_text("甲。乙。\n", encoding="utf-8")
    result = _run(tmp_path / "missing.md", bad, violation, tmp_path)
    assert result.returncode == 2
    assert violation.read_text(encoding="utf-8") == "甲。乙。\n"
    assert f"{_where(violation)}:1:2: error:" in result.stdout
    assert result.stderr.count("入力エラー") == 3
    assert _run().returncode == 0


@pytest.mark.parametrize("second_input", ["missing", "invalid-utf8", "directory"])
def test_fix_prepares_all_inputs_before_writing(tmp_path: pathlib.Path, second_input: str) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    third = tmp_path / "third.md"
    original = "甲。乙。\n".encode()
    for path in (first, third):
        path.write_bytes(original)
    if second_input == "invalid-utf8":
        second.write_bytes(b"\xff")
    elif second_input == "directory":
        second.mkdir()
    result = _run("--fix", first, second, third)
    assert result.returncode == 2
    assert "入力エラー" in result.stderr
    assert first.read_bytes() == original
    assert third.read_bytes() == original


def test_fix_writes_all_prepared_changes(tmp_path: pathlib.Path) -> None:
    paths = [tmp_path / "first.md", tmp_path / "second.md"]
    for path in paths:
        path.write_text("甲。乙。\n", encoding="utf-8")
    assert _run("--fix", *paths).returncode == 0
    for path in paths:
        assert path.read_text(encoding="utf-8") == "甲。\n乙。\n"
