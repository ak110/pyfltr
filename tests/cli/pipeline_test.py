import json
import logging
import pathlib
import unittest.mock

import pytest

import pyfltr.cli.main
import pyfltr.cli.pipeline
import pyfltr.config.config
import pyfltr.output.heartbeat
import pyfltr.run_options


def test_run_reports_subproject_toml_error(tmp_path: pathlib.Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pyfltr]\nrespect-gitignore = false\nsubproject-use-gitignore = false\n", encoding="utf-8"
    )
    child = tmp_path / "child"
    child.mkdir()
    invalid = child / "pyproject.toml"
    invalid.write_text("[tool.pyfltr\n", encoding="utf-8")
    target = child / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        code = pyfltr.cli.main.run(["run", str(target), "--work-dir", str(tmp_path), "--commands=mypy", "--no-cache"])
    assert code == 1
    assert str(invalid) in caplog.text
    assert "該当位置をTOMLの構文に直し" in caplog.text
    assert "Traceback" not in caplog.text


def _make_args(tmp_path: pathlib.Path, *, commands: list[str] | None) -> pyfltr.run_options.RunOptions:
    target = tmp_path / "sample.md"
    target.write_text("# Title\n", encoding="utf-8")
    return pyfltr.run_options.RunOptions.from_values(
        {
            "output_format": "jsonl",
            "output_file": None,
            "format_source": None,
            "stream": False,
            "no_clear": True,
            "no_ui": True,
            "ui": False,
            "targets": [target],
            "commands": commands,
            "exit_zero_even_if_formatted": True,
            "include_fix_stage": False,
            "fail_fast": False,
            "jobs": None,
            "no_exclude": False,
            "no_gitignore": True,
            "allow_external_paths": False,
            "human_readable": False,
            "shuffle": False,
            "verbose": False,
            "ci": False,
            "only_failed": False,
            "from_run": None,
            "changed_since": None,
            "no_archive": True,
            "no_cache": True,
            "subcommand": "run",
        }
    )


def _run_and_read_jsonl(
    monkeypatch,
    capsys,
    tmp_path: pathlib.Path,
    commands: list[str],
    *,
    args_commands: list[str] | None,
    enabled: set[str] | None = None,
) -> list[dict]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("pyfltr.cli.pipeline.run_commands_with_cli", lambda *args, **kwargs: [])
    config = pyfltr.config.config.create_default_config()
    config.values["respect-gitignore"] = False
    config.values["subproject-use-gitignore"] = False
    for command in enabled or set():
        config.values[command] = True

    pyfltr.cli.pipeline.run_pipeline(_make_args(tmp_path, commands=args_commands), commands, config)

    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    for record in records:
        if record.get("kind") == "warning":
            assert record["msg"] not in captured.err
    return records


def _summary_warning_count(records: list[dict]) -> int:
    summary = next(record for record in records if record.get("kind") == "summary")
    return summary.get("warnings", 0)


def test_unmet_commands_warning_emitted_when_commands_explicit(monkeypatch, capsys, tmp_path) -> None:
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint"],
        args_commands=["textlint"],
    )

    warnings = [record for record in records if record.get("kind") == "warning"]
    assert warnings == [
        {
            "kind": "warning",
            "source": "commands",
            "msg": "--commandsで指定されたが有効化されていないため未実行のコマンドがあります: textlint",
            "hint": (
                "--enable=textlint または pyproject.toml [tool.pyfltr] で指定したコマンドを true に設定して有効化してください。"
            ),
        }
    ]
    assert _summary_warning_count(records) == len(warnings)


def test_unmet_commands_warning_not_emitted_without_explicit_commands(monkeypatch, capsys, tmp_path) -> None:
    records = _run_and_read_jsonl(monkeypatch, capsys, tmp_path, ["textlint"], args_commands=None)

    assert not [record for record in records if record.get("source") == "commands"]
    warnings = [record for record in records if record.get("kind") == "warning"]
    assert _summary_warning_count(records) == len(warnings)


def test_unmet_commands_warning_not_emitted_when_all_enabled(monkeypatch, capsys, tmp_path) -> None:
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint"],
        args_commands=["textlint"],
        enabled={"textlint"},
    )

    assert not [record for record in records if record.get("source") == "commands"]


def test_unmet_commands_warning_groups_multiple_commands(monkeypatch, capsys, tmp_path) -> None:
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["textlint", "markdownlint", "typos"],
        args_commands=["textlint,markdownlint,typos"],
        enabled={"typos"},
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert warning["msg"].endswith("textlint, markdownlint")
    assert warning["hint"].startswith("--enable=textlint,markdownlint")


def test_unmet_commands_warning_not_emitted_for_alias_with_any_enabled(monkeypatch, capsys, tmp_path) -> None:
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit"],
        enabled={"pnpm-audit"},
    )

    assert not [record for record in records if record.get("source") == "commands"]


def test_unmet_commands_warning_emitted_for_alias_with_none_enabled(monkeypatch, capsys, tmp_path) -> None:
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit"],
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert "uv-audit" in warning["msg"]
    assert "yarn-audit" in warning["msg"]


def test_unmet_commands_warning_emitted_for_explicit_command_with_alias(monkeypatch, capsys, tmp_path) -> None:
    records = _run_and_read_jsonl(
        monkeypatch,
        capsys,
        tmp_path,
        ["uv-audit", "pnpm-audit", "npm-audit", "yarn-audit"],
        args_commands=["audit,uv-audit"],
        enabled={"pnpm-audit"},
    )

    warning = next(record for record in records if record.get("source") == "commands")
    assert warning["msg"].endswith("uv-audit")


def test_can_use_ui() -> None:
    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=True),
        unittest.mock.patch("sys.stdout.isatty", return_value=True),
    ):
        assert pyfltr.cli.pipeline.can_use_ui() is True

    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=False),
        unittest.mock.patch("sys.stdout.isatty", return_value=True),
    ):
        assert pyfltr.cli.pipeline.can_use_ui() is False

    with (
        unittest.mock.patch("sys.stdin.isatty", return_value=True),
        unittest.mock.patch("sys.stdout.isatty", return_value=False),
    ):
        assert pyfltr.cli.pipeline.can_use_ui() is False
