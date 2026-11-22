import argparse
import logging
import pathlib
import sys
import typing

import pyfltr.cli.output_format
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.grep_.operations
import pyfltr.grep_.scanner
import pyfltr.output.auxiliary
import pyfltr.output.logging_


def target_request(args: argparse.Namespace) -> pyfltr.grep_.operations.TargetRequest:
    return pyfltr.grep_.operations.TargetRequest.from_paths(
        args.paths or [], args.type, args.glob, args.no_exclude, args.no_gitignore
    )


def pattern_arguments(args: argparse.Namespace) -> dict[str, typing.Any]:
    return {
        "targets": target_request(args),
        **pyfltr.grep_.operations.PatternOptions.arguments(
            (args.fixed_strings, args.ignore_case, args.smart_case, args.word_regexp, args.line_regexp, args.multiline),
            (args.before_context, args.after_context, args.context),
            args.encoding,
            args.max_filesize,
        ),
    }


def add_common_output_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-exclude",
        action="store_true",
        help="exclude / extend-exclude による除外を無効化する。",
    )
    parser.add_argument(
        "--no-gitignore",
        action="store_true",
        help=".gitignore による除外を無効化する。",
    )
    parser.add_argument(
        "--output-format",
        choices=pyfltr.output.auxiliary.OUTPUT_FORMATS,
        default=None,
        help=(
            "出力形式を指定する（text / json / jsonl、既定: text）。"
            f"未指定時は環境変数 {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} を採用し、"
            f"{' / '.join(pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)} のいずれかが設定されていれば jsonl を採用する。"
        ),
    )
    parser.add_argument(
        "--output-file",
        type=pathlib.Path,
        default=None,
        help="JSONL / json出力先ファイル。未指定時は stdout に出力する。",
    )


def setup_output(parser: argparse.ArgumentParser, args: argparse.Namespace) -> pyfltr.cli.output_format.OutputFormatResolution:
    resolution = pyfltr.cli.output_format.resolve_output_format(
        parser,
        args.output_format,
        valid_values=pyfltr.output.auxiliary.VALID_OUTPUT_FORMATS,
        ai_agent_default="jsonl",
    )
    output_format = resolution.format

    if output_format == "text":
        pyfltr.output.logging_.configure_text_output(sys.stdout)
    else:
        pyfltr.output.logging_.configure_text_output(sys.stderr, level=logging.WARNING)

    if output_format == "jsonl":
        if args.output_file is not None:
            pyfltr.output.logging_.configure_structured_output(args.output_file)
        else:
            pyfltr.output.logging_.configure_structured_output(sys.stdout)
    else:
        pyfltr.output.logging_.configure_structured_output(None)

    return resolution


def print_json(payload: dict[str, typing.Any], output_file: pathlib.Path | None) -> None:
    pyfltr.output.auxiliary.print_json(payload, output_file, indent=2)
