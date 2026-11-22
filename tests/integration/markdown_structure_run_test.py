import json
import pathlib
import shlex
import subprocess
import sys

_DOCUMENT = "# 見出し\n\n一文目である。二文目である。\n\n説明（補足（内側））を書く。\n"


def _run(tmp_path: pathlib.Path, *extra: str, targets: tuple[str, ...] = ("doc.md",)) -> tuple[int, list[dict]]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyfltr",
            "run",
            "--work-dir",
            str(tmp_path),
            "--commands=markdown-structure",
            "--no-quiet",
            "--output-format=jsonl",
            *extra,
            *targets,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        check=False,
        timeout=60,
        cwd=tmp_path,
    )
    return proc.returncode, [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]


def _project(tmp_path: pathlib.Path, *, enabled: bool) -> pathlib.Path:
    settings = "[tool.pyfltr]\nrespect-gitignore = false\n" + ("markdown-structure = true\n" if enabled else "")
    (tmp_path / "pyproject.toml").write_text(settings, encoding="utf-8")
    (tmp_path / "doc.md").write_text(_DOCUMENT, encoding="utf-8")
    return tmp_path


def _messages(records: list[dict]) -> list[tuple[int, str]]:
    return [
        (message["line"], message["msg"])
        for record in records
        if record.get("kind") == "diagnostic" and record.get("command") == "markdown-structure"
        for message in record["messages"]
    ]


def test_enabled_check_reports_both_violations(tmp_path: pathlib.Path) -> None:
    _project(tmp_path, enabled=True)
    returncode, records = _run(tmp_path, "--no-fix")
    assert returncode == 1
    lines = [line for line, _ in _messages(records)]
    assert lines == [3, 5]
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == _DOCUMENT


def test_fix_stage_resolves_sentence_break_and_keeps_nested_parentheses(tmp_path: pathlib.Path) -> None:
    _project(tmp_path, enabled=True)
    returncode, records = _run(tmp_path)
    assert returncode == 1
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == (
        "# 見出し\n\n一文目である。\n二文目である。\n\n説明（補足（内側））を書く。\n"
    )
    messages = _messages(records)
    assert len(messages) == 1
    assert "入れ子" in messages[0][1]


def test_disabled_check_does_not_report(tmp_path: pathlib.Path) -> None:
    _project(tmp_path, enabled=False)
    returncode, records = _run(tmp_path)
    assert returncode == 0
    assert not _messages(records)
    assert (tmp_path / "doc.md").read_text(encoding="utf-8") == _DOCUMENT


def test_unreadable_file_is_reported_and_retried_with_other_failures(tmp_path: pathlib.Path) -> None:
    _project(tmp_path, enabled=True)
    (tmp_path / "bad.md").write_bytes(b"\xff")
    returncode, records = _run(tmp_path, "--no-fix", targets=("bad.md", "doc.md"))
    assert returncode == 1
    files = {
        record["file"]
        for record in records
        if record.get("kind") == "diagnostic" and record.get("command") == "markdown-structure"
    }
    assert files == {"bad.md", "doc.md"}
    (command,) = [
        record for record in records if record.get("kind") == "command" and record.get("command") == "markdown-structure"
    ]
    retry_targets = {pathlib.Path(arg).name for arg in shlex.split(command["retry_command"])[-2:]}
    assert retry_targets == {"bad.md", "doc.md"}
