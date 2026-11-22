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
