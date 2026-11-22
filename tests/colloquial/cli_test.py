import pathlib
import subprocess
import sys

import pytest

import pyfltr.colloquial.check
import pyfltr.colloquial.cli
import pyfltr.paths


def _run(*paths: pathlib.Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pyfltr.colloquial", *(str(p) for p in paths)],
        capture_output=True,
        text=True,
        check=False,
    )


_REGEX_METACHARS = "[]()?*+{}|\\^$."


def _deny_sample(*, require_replacement: bool = False) -> tuple[str, str | None]:
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
    target = tmp_path / "clean.md"
    target.write_text("plain ASCII content without any flagged phrase.\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 0
    assert result.stdout == ""


def test_natural_written_actions_exit_zero(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "natural.md"
    target.write_text(
        "設定ファイルを作る。\n候補を一覧へ入れた。\n結果を画面に出す。\nカードを端末に差し込む。\n"
        "一時ファイルを捨てた。\n結果をファイルへ書き出す。\n不要なファイルを消す。\n実行結果を見る。\n",
        encoding="utf-8",
    )
    result = _run(target)
    assert result.returncode == 0
    assert not result.stdout
    assert not result.stderr


def test_informal_and_metaphorical_expressions_keep_cli_candidates(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "informal.md"
    target.write_text("めっちゃ速い。\n処理をやる。\nジョブが走る。\n入力を弾く。\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 4
    for line, (match, replacement) in zip(
        lines, [("めっちゃ", "程度を具体化する"), ("やる", "実施する"), ("走る", "実行"), ("弾く", "拒否する")], strict=True
    ):
        assert f"[{match}] -> [{replacement}]" in line
    assert not result.stderr


def test_detection_exits_one_with_expected_format(tmp_path: pathlib.Path) -> None:
    deny_line, _ = _deny_sample()
    assert deny_line, "denylistから単純なリテラル行を取得できなかった"
    target = tmp_path / "hit.md"
    target.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{pyfltr.paths.normalize_separators(target)}:1:")
    assert f"[{deny_line}]" in lines[0]


def test_noun_gyou_followed_by_particles_exits_zero(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "noun.md"
    target.write_text(
        "出力が`該当0件:`の行から成る。\n次の行から読む。\n次の行こそ重要。\nこの行くらいは残す。\n"
        "対象がどの行かを示す。\nどの行か、確認する。\nこの行かあの行。\nどの行かによる。\n",
        encoding="utf-8",
    )
    result = _run(target)
    assert result.returncode == 0
    assert not result.stdout
    assert not result.stderr


@pytest.mark.parametrize(
    ("sentence", "expected_match"),
    [
        ("行かない。", "行か"),
        ("行きます。", "行き"),
        ("行こう。", "行こ"),
        ("行くらしい。", "行く"),
        ("行かん。", "行か"),
        ("行かざるを得ない。", "行か"),
        ("行かにゃならん。", "行か"),
        ("行かへん。", "行か"),
        ("行かしめる。", "行か"),
        ("行かば、", "行か"),
        ("行かむ。", "行か"),
        ("行かじ。", "行か"),
        ("行かう。", "行か"),
        ("行かっしゃい。", "行か"),
        ("行かい。", "行か"),
        ("行かー。", "行か"),
    ],
)
def test_verb_iku_keeps_detection(tmp_path: pathlib.Path, sentence: str, expected_match: str) -> None:
    target = tmp_path / "verb.md"
    target.write_text(sentence + "\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 1
    assert f"[{expected_match}]" in result.stdout
    assert not result.stderr


def test_replacement_candidate_included(tmp_path: pathlib.Path) -> None:
    deny_line, replacement = _deny_sample(require_replacement=True)
    assert deny_line and replacement, "置換候補付きの単純なリテラル行を取得できなかった"
    target = tmp_path / "hit.md"
    target.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 1
    assert f"-> [{replacement}]" in result.stdout


def test_multiple_files_report_all_hits(tmp_path: pathlib.Path) -> None:
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
    assert any(line.startswith(f"{pyfltr.paths.normalize_separators(target1)}:") for line in lines)
    assert any(line.startswith(f"{pyfltr.paths.normalize_separators(target2)}:") for line in lines)


def test_missing_file_is_skipped(tmp_path: pathlib.Path) -> None:
    deny_line, _ = _deny_sample()
    assert deny_line
    missing = tmp_path / "missing.md"
    target = tmp_path / "hit.md"
    target.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(missing, target)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"{pyfltr.paths.normalize_separators(target)}:")
    assert pyfltr.paths.normalize_separators(missing) in result.stderr
    assert "読み取り権限を確認してください" in result.stderr


def test_no_replacement_candidate_shows_rewrite_guidance(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deny = tmp_path / "deny.txt"
    deny.write_text("ぶっちゃけ\n", encoding="utf-8")
    monkeypatch.setattr(pyfltr.colloquial.check, "DENY_PATH", deny)
    monkeypatch.setattr(pyfltr.colloquial.check, "ALLOW_PATH", tmp_path / "missing-allow.txt")
    target = tmp_path / "hit.md"
    target.write_text("本文でぶっちゃけ述べる。\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["pyfltr.colloquial", str(target)])
    assert pyfltr.colloquial.cli.main() == 1
    out = capsys.readouterr().out
    assert "[ぶっちゃけ] -> (" in out
    assert "書き言葉" in out


def test_cli_normalizes_backslash_in_filename(tmp_path: pathlib.Path) -> None:
    deny_line, _ = _deny_sample()
    assert deny_line
    target = tmp_path / r"nested\hit.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"本文に{deny_line}該当する。\n", encoding="utf-8")
    result = _run(target)
    assert result.returncode == 1
    lines = result.stdout.splitlines()
    assert len(lines) == 1
    normalized = pyfltr.paths.normalize_separators(target)
    assert lines[0].startswith(f"{normalized}:1:")
    assert "\\" not in lines[0]
