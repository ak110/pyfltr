"""`grep` / `replace`サブコマンドの共通CLI引数と実行前処理。

`.claude/skills/grep-replace/SKILL.md`「引数体系の同一性」節に従い、`grep`と`replace`は
`--no-exclude` / `--no-gitignore` / `--output-format` / `--output-file`を共有オプションとして
受理する。両サブコマンドの`register_subparsers` / `execute_*`冒頭で共通の
引数登録・出力形式解決・設定ロード・対象ファイル展開を本モジュールへ集約する。
"""

import argparse
import json
import logging
import pathlib
import sys
import typing

import pyfltr.cli.output_format
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.grep_.scanner

_OUTPUT_FORMATS: tuple[str, ...] = ("text", "json", "jsonl")
_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(_OUTPUT_FORMATS)


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
        choices=_OUTPUT_FORMATS,
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
        valid_values=_VALID_OUTPUT_FORMATS,
        ai_agent_default="jsonl",
    )
    output_format = resolution.format

    if output_format == "text":
        pyfltr.cli.output_format.configure_text_output(sys.stdout)
    else:
        pyfltr.cli.output_format.configure_text_output(sys.stderr, level=logging.WARNING)

    if output_format == "jsonl":
        if args.output_file is not None:
            pyfltr.cli.output_format.configure_structured_output(args.output_file)
        else:
            pyfltr.cli.output_format.configure_structured_output(sys.stdout)
    else:
        pyfltr.cli.output_format.configure_structured_output(None)

    return resolution


def print_json(payload: dict[str, typing.Any], output_file: pathlib.Path | None) -> None:
    """単発JSONをstdoutまたは`--output-file`に書く。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def load_config_and_expand_targets(
    args: argparse.Namespace,
) -> tuple[pyfltr.config.config.Config, list[pathlib.Path]] | None:
    """設定をロードし、`--no-exclude` / `--no-gitignore`適用後に対象ファイルを展開する。

    設定ロード失敗（`ValueError` / `OSError`）時は標準エラーへメッセージを出力し`None`を返す。
    呼び出し側は戻り値が`None`のとき`return 1`でCLI終了コードへ変換する。
    """
    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"設定エラー: {exc}\n")
        return None
    if args.no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if args.no_gitignore:
        config.values["respect-gitignore"] = False

    targets = list(args.paths) if args.paths else []
    expanded = pyfltr.command.targets.expand_all_files(targets, config)
    expanded = pyfltr.grep_.scanner.filter_files_by_type(expanded, args.type)
    expanded = pyfltr.grep_.scanner.filter_by_globs(expanded, args.glob)
    return config, expanded
