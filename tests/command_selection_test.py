"""command_selection.pyのテスト。"""

import argparse

import pytest

import pyfltr.cli.command_selection
import pyfltr.config.config


def _make_args(subcommand: str, **kwargs: object) -> argparse.Namespace:
    """`apply_subcommand_defaults`へ渡す最小Namespaceを生成する。"""
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
    """実行モードごとのfixステージと終了コード既定値を解決する。"""
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
    """fastは未指定時だけ既定コマンドを設定する。"""
    args = _make_args("fast", commands=commands)

    pyfltr.cli.command_selection.apply_subcommand_defaults(args)

    assert args.commands == expected


def test_apply_subcommand_defaults_preserves_explicit_quiet() -> None:
    """明示されたquiet値を上書きしない。"""
    args = _make_args("ci", quiet=True)

    pyfltr.cli.command_selection.apply_subcommand_defaults(args)

    assert args.quiet is True


def test_validate_commands_accepts_registered_command() -> None:
    """登録済みコマンドを受理する。"""
    config = pyfltr.config.config.create_default_config()

    pyfltr.cli.command_selection.validate_commands(["mypy"], config)


def test_validate_commands_rejects_unknown_command_with_suggestion() -> None:
    """未知コマンドを近接候補付きで拒否する。"""
    config = pyfltr.config.config.create_default_config()

    with pytest.raises(ValueError, match=r"コマンドが見つかりません: mypyy。もしかして: mypy"):
        pyfltr.cli.command_selection.validate_commands(["mypyy"], config)


def test_validate_commands_rejects_non_command_config_key() -> None:
    """コマンドではない設定キーを拒否する。"""
    config = pyfltr.config.config.create_default_config()

    with pytest.raises(ValueError, match=r"^コマンドが見つかりません: archive"):
        pyfltr.cli.command_selection.validate_commands(["archive"], config)
