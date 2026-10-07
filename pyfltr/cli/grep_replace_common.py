"""`grep` / `replace`サブコマンドの共通CLI引数と実行前処理。

`.claude/skills/grep-replace/SKILL.md`「引数体系の同一性」節に従い、`grep`と`replace`は
`--no-exclude` / `--no-gitignore` / `--output-format` / `--output-file`を共有オプションとして
受理する。両サブコマンドの`register_subparsers` / `execute_*`冒頭で共通の
引数登録・出力形式解決・設定ロード・対象ファイル展開を本モジュールへ集約する。
"""

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
    """共通のCLI引数を検索・置換の対象要求へ変換する。"""
    return pyfltr.grep_.operations.TargetRequest.from_paths(
        args.paths or [], args.type, args.glob, args.no_exclude, args.no_gitignore
    )


def pattern_arguments(args: argparse.Namespace) -> dict[str, typing.Any]:
    """両CLI入口の共通設定を要求のキーワード引数へ変換する。"""
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
    """`--no-exclude` / `--no-gitignore` / `--output-format` / `--output-file`を登録する。"""
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
    """出力形式を解決し、text logger / structured loggerの出力先を設定する。

    jsonl / jsonの場合はstdout専有のためtext_loggerをstderrへ抑止する
    （json時は最後に1回dumpするためstructured loggerのハンドラー設定は行わない）。
    """
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
    """単発JSONをstdoutまたは`--output-file`に書く。"""
    pyfltr.output.auxiliary.print_json(payload, output_file, indent=2)
