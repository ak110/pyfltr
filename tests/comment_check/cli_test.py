import json
from pathlib import Path

import pytest

from pyfltr.comment_check.cli import main


def test_ratchet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    source = Path("sample.py")
    files = [str(source)]
    source.write_text("# external constraint\n", encoding="utf-8")
    assert main(["--init-baseline", *files]) == 0
    assert main(files) == 0

    source.write_text("# different constraint\n", encoding="utf-8")
    assert main(files) == 1
    source.write_text("# external constraint\n# external   constraint\n", encoding="utf-8")
    assert main(files) == 1

    source.write_text("answer = 42\n", encoding="utf-8")
    assert main(files) == 0
    assert main(["--update-baseline", *files]) == 0
    assert json.loads(Path(".pyfltr-comment-baseline.json").read_text(encoding="utf-8")) == {}
    source.write_text("# external constraint\n", encoding="utf-8")
    assert main(files) == 1


def test_read_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["missing.py"]) == 1
    output = capsys.readouterr().out
    assert output.startswith("missing.py:1:1: error: コメントを検査できません:")
    assert "missing.py" in output.split("検査できません:", 1)[1]


def test_violation_guidance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    Path("sample.py").write_text("# unexpected\n", encoding="utf-8")
    assert main(["sample.py"]) == 1
    output = capsys.readouterr().out
    assert output.startswith("sample.py:1:1: error: ")
    for guidance in (
        "pyfltr: allow-comment",
        "--allow-pattern",
        'pyfltr run "sample.py" --commands=comment-check --enable=comment-check --comment-check-args=--init-baseline',
    ):
        assert guidance in output


@pytest.mark.parametrize("operation", [[], ["--init-baseline"]])
def test_skips_nested_repository_and_empty_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: list[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("vendor/lib").mkdir(parents=True)
    Path("vendor/lib/.git").write_text("gitdir: ../../.git/modules/lib\n", encoding="utf-8")
    Path("vendor/lib/code.py").write_text("# vendored\n", encoding="utf-8")
    Path("pending").mkdir()
    Path("sample.py").write_text("x = 1\n", encoding="utf-8")
    assert main([*operation, "vendor/lib", "pending", "sample.py"]) == 0


def test_init_baseline_keeps_other_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    Path("first.py").write_text("# first\n", encoding="utf-8")
    Path("second.py").write_text("# second\n", encoding="utf-8")
    assert main(["--init-baseline", "first.py"]) == 0
    assert main(["--init-baseline", "second.py"]) == 0
    assert main(["first.py", "second.py"]) == 0


@pytest.mark.parametrize(
    ("argv", "baseline", "expected"),
    [
        (["src"], None, "srcはディレクトリです"),
        (["--init-baseline", "src"], None, "srcはディレクトリです"),
        (["--allow-pattern", "(", "sample.py"], None, "--allow-patternの正規表現'('が不正です"),
        (["sample.py"], "{broken", "JSONとして読めません"),
        (["sample.py"], "[]", "JSONオブジェクトではありません"),
        (["sample.py"], '{"sample.py": {"x": 0}}', "正の整数ではありません"),
    ],
)
def test_fatal_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    baseline: str | None,
    expected: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    Path("src").mkdir()
    Path("src/app.py").write_text("x = 1\n", encoding="utf-8")
    Path("sample.py").write_text("x = 1\n", encoding="utf-8")
    if baseline is not None:
        Path(".pyfltr-comment-baseline.json").write_text(baseline, encoding="utf-8")
    assert main(argv) == 2
    error = capsys.readouterr().err
    assert expected in error
    assert "直して" in error or "指定するか" in error
