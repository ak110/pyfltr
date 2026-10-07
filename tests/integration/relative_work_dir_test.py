"""相対work-dirを使うCLI境界と対象解決の結合を検証する。"""

import json
import pathlib

import pytest

import pyfltr.cli.main
import pyfltr.command.targets


@pytest.mark.parametrize("custom_command", [False, True])
def test_relative_work_dir_preserves_changed_target(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], custom_command: bool
) -> None:
    """custom引数の再解析を行っても、起動cwd相対の変更対象を検査する。"""
    project = tmp_path / "project"
    project.mkdir()
    config = "[tool.pyfltr]\nruff-check = true\n"
    if custom_command:
        config += '\n[tool.pyfltr.custom-commands.extra]\ntype = "linter"\npath = "unused-extra"\n'
    (project / "pyproject.toml").write_text(config, encoding="utf-8")
    (project / "sample.py").write_text("value = 1\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pyfltr.command.targets, "_get_changed_files", lambda *_args, **_kwargs: ["sample.py"])

    assert (
        pyfltr.cli.main.run(
            [
                "run",
                "--work-dir=project",
                "project/sample.py",
                "--changed-since=HEAD",
                "--commands=ruff-check",
                "--no-fix",
                "--no-cache",
                "--no-archive",
                "--output-format=jsonl",
                "--no-quiet",
            ]
        )
        == 0
    )
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    command = next(record for record in records if record["kind"] == "command")
    assert command["status"] == "succeeded"
    assert command["files"] == 1
    summary = next(record for record in records if record["kind"] == "summary")
    assert summary["files_reached"] == 1
    assert summary["completed_commands"] == ["ruff-check"]
