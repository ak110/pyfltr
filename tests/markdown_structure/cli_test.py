"""`python -m pyfltr.markdown_structure`の公開CLI契約のテスト。

検出条件と除外条件はdotfilesの`scripts/check_markdown_structure_test.py`が確かめていた入力を引き継ぐ。
"""

import pathlib
import subprocess
import sys

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
    """違反を報告し、`--fix`は改行とリスト継続の字下げだけを加え、再実行しても変えない。"""
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
    """コード・HTMLコメント・frontmatter・見出し・表・引用・逐語引用の段落などは修正しない。"""
    path = tmp_path / "doc.md"
    path.write_text(source, encoding="utf-8")
    assert _run("--fix", path).returncode == 0
    assert path.read_text(encoding="utf-8") == source


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
    """全角丸括弧の入れ子は自動修正せず、位置付きのerrorを標準出力へ報告する。"""
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
    """表のセルの括弧の深さは表全体で数え、後続の段落の位置を正しく報告する。"""
    path = tmp_path / "table.md"
    path.write_text(source, encoding="utf-8")
    result = _run(path)
    assert result.returncode == int(bool(locations))
    assert result.stdout.splitlines() == [
        f"{_where(path)}{location} error: 全角丸括弧の入れ子を解消する" for location in locations
    ]
    assert path.read_text(encoding="utf-8") == source


def test_fixtures_directory_is_not_excluded(tmp_path: pathlib.Path) -> None:
    """dotfiles固有だった`fixtures`ディレクトリの除外を内蔵検査へ持ち込まない。"""
    path = tmp_path / "fixtures" / "request.md"
    path.parent.mkdir()
    path.write_text("甲。乙。\n", encoding="utf-8")
    assert _run(path).returncode == 1


def test_input_error_takes_precedence_and_other_files_are_checked(tmp_path: pathlib.Path) -> None:
    """入力エラーは終了コード2とし、他のファイルの検査結果も報告する。"""
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
    """入力エラーがある場合は、他のファイルも書き換えない。"""
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
    """複数ファイルの修正をすべて書き込む。"""
    paths = [tmp_path / "first.md", tmp_path / "second.md"]
    for path in paths:
        path.write_text("甲。乙。\n", encoding="utf-8")
    assert _run("--fix", *paths).returncode == 0
    for path in paths:
        assert path.read_text(encoding="utf-8") == "甲。\n乙。\n"
