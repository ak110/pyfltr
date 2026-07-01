"""`python -m pyfltr.colloquial` の統合テスト。

dispatcherが実際に起動する経路（subprocess）を通して、出力フォーマットと
終了コードを検証する。
"""

import pathlib
import subprocess
import sys

import pyfltr.colloquial.check


def _run(*paths: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pyfltr.colloquial", *(str(p) for p in paths)],
        capture_output=True,
        text=True,
        check=False,
    )


_REGEX_METACHARS = "[]()?*+{}|\\^$."


def _deny_sample(*, require_replacement: bool = False) -> tuple[str, str | None]:
    """denylistから正規表現記号を含まない単純なリテラル行を探して返す。

    `words.txt`は正規表現の集合であり、記号を含む行はそのままテキストへ埋め込めない。
    戻り値は`(パターン文字列, 置換候補)`のタプル。
    `require_replacement=True`のときは置換候補列（タブ区切り）を持つ行に限定する。
    """
    for line in pyfltr.colloquial.check.DENY_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head, sep, tail = stripped.partition("\t")
        if any(c in head for c in _REGEX_METACHARS):
            continue
        if require_replacement and not (sep and tail):
            continue
        return head, (tail or None)
    return "", None


def test_no_detection_exits_zero(tmp_path: pathlib.Path) -> None:
    """検出なしのときexit 0でstdoutが空になる。"""
    target = tmp_path / "clean.md"
    target.write_text("plain ASCII content without any flagged phrase.\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 0
    assert result.stdout == ""


def test_detection_exits_one_with_expected_format(tmp_path: pathlib.Path) -> None:
    """検出ありのときexit 1でstdoutに`path:line:col: [match] excerpt`形式の行が出る。"""
    deny_line, _ = _deny_sample()
    assert deny_line, "denylistから単純なリテラル行を取得できなかった"
    target = tmp_path / "hit.md"
    target.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{target}:1:")
    assert f"[{deny_line}]" in lines[0]


def test_replacement_candidate_included(tmp_path: pathlib.Path) -> None:
    """置換候補があるとき`-> [replacement]`がstdoutに含まれる。"""
    deny_line, replacement = _deny_sample(require_replacement=True)
    assert deny_line and replacement, "置換候補付きの単純なリテラル行を取得できなかった"
    target = tmp_path / "hit.md"
    target.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 1
    assert f"-> [{replacement}]" in result.stdout


def test_multiple_files_report_all_hits(tmp_path: pathlib.Path) -> None:
    """複数ファイル指定時、全ての違反が列挙される。"""
    deny_line, _ = _deny_sample()
    assert deny_line
    target1 = tmp_path / "hit1.md"
    target2 = tmp_path / "hit2.md"
    target1.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    target2.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(target1, target2)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert any(line.startswith(f"{target1}:") for line in lines)
    assert any(line.startswith(f"{target2}:") for line in lines)


def test_missing_file_is_skipped(tmp_path: pathlib.Path) -> None:
    """存在しないファイルはスキップされ、他ファイルの検出結果は出力される。"""
    deny_line, _ = _deny_sample()
    assert deny_line
    missing = tmp_path / "missing.md"
    target = tmp_path / "hit.md"
    target.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(missing, target)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{target}:")
