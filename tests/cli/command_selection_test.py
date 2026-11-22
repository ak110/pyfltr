import argparse
import json
import subprocess

import pytest

import pyfltr.cli.command_selection
import pyfltr.cli.main
import pyfltr.config.config


def _make_args(subcommand: str, **kwargs: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "subcommand": subcommand,
        "commands": None,
        "exit_zero_even_if_formatted": False,
        "quiet": None,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


@pytest.mark.parametrize(
    ("subcommand", "include_fix_stage", "exit_zero_even_if_formatted"),
    [
        ("run", True, True),
        ("ci", False, False),
    ],
)
def test_apply_subcommand_defaults_resolves_execution_mode(
    subcommand: str,
    include_fix_stage: bool,
    exit_zero_even_if_formatted: bool,
) -> None:
    args = _make_args(subcommand)

    pyfltr.cli.command_selection.apply_subcommand_defaults(args)

    assert vars(args)["include_fix_stage"] is include_fix_stage
    assert args.exit_zero_even_if_formatted is exit_zero_even_if_formatted


@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        (None, ["fast"]),
        (["mypy"], ["mypy"]),
    ],
)
def test_apply_subcommand_defaults_preserves_explicit_fast_commands(
    commands: list[str] | None,
    expected: list[str],
) -> None:
    args = _make_args("fast", commands=commands)

    pyfltr.cli.command_selection.apply_subcommand_defaults(args)

    assert args.commands == expected


def test_apply_subcommand_defaults_preserves_explicit_quiet() -> None:
    args = _make_args("ci", quiet=True)

    pyfltr.cli.command_selection.apply_subcommand_defaults(args)

    assert args.quiet is True


def test_validate_commands_accepts_registered_command(tmp_path, mocker, capsys) -> None:
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")
    proc = subprocess.CompletedProcess(["mypy"], returncode=1, stdout="", stderr="")
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=proc)

    rc = pyfltr.cli.main.run(
        ["ci", "--work-dir", str(tmp_path), "--output-format=jsonl", "--commands=mypy", "--enable=mypy", str(target)]
    )

    assert rc == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["command"] for record in records if record["kind"] == "command"] == ["mypy"]


def test_validate_commands_rejects_unknown_command_with_suggestion() -> None:
    config = pyfltr.config.config.create_default_config()

    with pytest.raises(ValueError, match=r"コマンドが見つかりません: mypyy。もしかして: mypy"):
        pyfltr.cli.command_selection.validate_commands(["mypyy"], config)


def test_validate_commands_rejects_non_command_config_key() -> None:
    config = pyfltr.config.config.create_default_config()

    with pytest.raises(ValueError, match=r"^コマンドが見つかりません: archive"):
        pyfltr.cli.command_selection.validate_commands(["archive"], config)
