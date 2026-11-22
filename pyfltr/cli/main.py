#!/usr/bin/env python3

import argparse
import contextlib
import importlib.metadata
import logging
import os
import pathlib
import sys
import typing

import pyfltr.cli.command_info
import pyfltr.cli.command_selection
import pyfltr.cli.config_subcmd
import pyfltr.cli.grep_subcmd
import pyfltr.cli.parser
import pyfltr.cli.pipeline
import pyfltr.cli.replace_subcmd
import pyfltr.cli.runs
import pyfltr.cli.shell_completion
import pyfltr.command.env
import pyfltr.state.runs

logger = logging.getLogger(__name__)

os.environ.pop("COLUMNS", None)
os.environ.pop("LINES", None)


def _reconfigure_stdio_to_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> typing.NoReturn:
    """エントリポイント。

    PATH重複排除はCLI経路でのみ呼ぶことで、ライブラリ用途で意図せず別アプリの
    `os.environ` を書き換えないようにする。
    重複排除の規則は `pyfltr.command.env.dedupe_environ_path` のdocstringを参照する。
    """
    _reconfigure_stdio_to_utf8()
    pyfltr.command.env.dedupe_environ_path(os.environ)
    exit_code = run()
    logger.debug(f"{exit_code=}")
    sys.exit(exit_code)


def run(sys_args: typing.Sequence[str] | None = None) -> int:
    if sys_args is None:
        sys_args = sys.argv[1:]

    if len(sys_args) == 1 and sys_args[0] in ("-V", "--version"):
        print(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    pyfltr.cli.parser.preflight_tool_name_as_subcommand(sys_args)

    parser = pyfltr.cli.parser.build_parser()
    # 位置引数解析が早期終了する挙動があり（Python 3.12〜3.15でも再現する）、`--output-format=jsonl`等が
    # `parse_known_args`で残余を回収し、`paths`へ統合する経路で回避する。
    if sys_args and sys_args[0] in ("grep", "replace"):
        args, extras = parser.parse_known_args(list(sys_args))
        unknown_options = [token for token in extras if token.startswith("-")]
        if unknown_options:
            parser.error(f"unrecognized arguments: {' '.join(unknown_options)}")
        if extras and hasattr(args, "paths"):
            args.paths = list(args.paths) + [pathlib.Path(p) for p in extras]
    else:
        args = parser.parse_args(list(sys_args))
    subcommand = args.subcommand
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO, format="%(message)s")

    spec = next(spec for spec in pyfltr.cli.parser.SUBCOMMANDS if spec.name == subcommand)
    if spec.execute is not None:
        return spec.execute(parser, args)

    pyfltr.cli.command_selection.apply_subcommand_defaults(args)

    if getattr(args, "no_fix", False):
        args.include_fix_stage = False

    original_cwd = os.getcwd()
    resolved_targets: list[pathlib.Path] | None = None
    if args.work_dir is not None:
        resolved_targets = [t.absolute() for t in args.targets]
        args.work_dir = pathlib.Path(args.work_dir).expanduser().resolve()

    # cli/pipeline→cli/parserの直接依存（循環importの原因）を回避する。
    def _reparse_with_custom(custom_commands: list[str]) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
        p = pyfltr.cli.parser.build_parser(custom_commands)
        a = p.parse_args(list(sys_args))
        pyfltr.cli.command_selection.apply_subcommand_defaults(a)
        return p, a

    return pyfltr.cli.pipeline.run_impl(
        parser,
        args,
        list(sys_args),
        resolved_targets,
        original_cwd=original_cwd,
        reparse_fn=_reparse_with_custom,
    )


if __name__ == "__main__":
    main()
