#!/usr/bin/env python3
"""pyfltr。"""

import argparse
import importlib.metadata
import logging
import os
import pathlib
import subprocess
import sys
import typing

import pyfltr.cli
import pyfltr.command
import pyfltr.config
import pyfltr.error_parser
import pyfltr.ui

logger = logging.getLogger(__name__)

# 環境変数を打ち消してリサイズに対応する
os.environ.pop("COLUMNS", None)
os.environ.pop("LINES", None)


def main() -> typing.NoReturn:
    """エントリポイント。"""
    exit_code = run()
    logger.debug(f"{exit_code=}")
    sys.exit(exit_code)


def build_parser() -> argparse.ArgumentParser:
    """引数パーサーを生成。"""
    parser = argparse.ArgumentParser(
        epilog="ドキュメント: https://ak110.github.io/pyfltr/\nllms.txt: https://ak110.github.io/pyfltr/llms.txt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--verbose", default=False, action="store_true", help="詳細な出力を表示します。")
    parser.add_argument(
        "--exit-zero-even-if-formatted",
        default=False,
        action="store_true",
        help="linters/testers にエラーがある場合のみ exit 1 とします。",
    )
    parser.add_argument(
        "--commands",
        default=",".join(pyfltr.config.BUILTIN_COMMAND_NAMES),
        help="カンマ区切りのコマンド一覧を指定します。(既定: %(default)s)",
    )
    parser.add_argument(
        "--generate-config",
        default=False,
        action="store_true",
        help="設定ファイルのサンプルを生成します(pyproject.toml の一部)。",
    )
    parser.add_argument("--ui", default=None, action="store_true", help="Textual UI を強制的に有効化します。")
    parser.add_argument("--no-ui", default=None, action="store_true", help="Textual UI を強制的に無効化します。")
    parser.add_argument(
        "--fix",
        default=False,
        action="store_true",
        help="fix モードで実行します(対応ツールに --fix 相当の引数を追加し、順次実行します)。",
    )
    parser.add_argument("--shuffle", default=False, action="store_true", help="ファイル順をシャッフルします。")
    parser.add_argument("--keep-ui", default=False, action="store_true", help="正常終了後も TUI を閉じずに維持します。")
    parser.add_argument("--ci", default=False, action="store_true", help="CI モードで動作します(--no-shuffle --no-ui 相当)。")
    parser.add_argument("--no-clear", default=False, action="store_true", help="実行前にターミナルをクリアしません。")
    parser.add_argument(
        "--work-dir",
        type=pathlib.Path,
        default=None,
        help="実行前に作業ディレクトリを変更します(既定: カレントディレクトリ)。",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="linters/testers の最大並列数を指定します(既定: 4、pyproject.toml でも設定可能です)。",
    )

    # 各コマンド用の引数追加オプション
    for command in pyfltr.config.BUILTIN_COMMANDS:
        parser.add_argument(
            f"--{command}-args",
            default="",
            help=f"{command} への追加引数を指定します。",
        )

    parser.add_argument(
        "targets",
        nargs="*",
        type=pathlib.Path,
        help="対象のファイルまたはディレクトリを指定します(既定: カレントディレクトリ)。",
    )
    parser.add_argument("--version", "-V", action="store_true", help="バージョンを表示します。")
    return parser


def run(sys_args: typing.Sequence[str] | None = None) -> int:
    """処理の実行。"""
    parser = build_parser()
    args = parser.parse_args(sys_args)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    # --work-dir: ターゲットパスを絶対パスに変換してからcwd変更
    original_cwd: str | None = None
    resolved_targets: list[pathlib.Path] | None = None
    if args.work_dir is not None:
        resolved_targets = [t.absolute() for t in args.targets]
        original_cwd = os.getcwd()
        os.chdir(args.work_dir)
    try:
        return _run_impl(parser, args, sys_args, resolved_targets)
    finally:
        if original_cwd is not None:
            os.chdir(original_cwd)


def _run_impl(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    sys_args: typing.Sequence[str] | None,
    resolved_targets: list[pathlib.Path] | None,
) -> int:
    """run()の内部実装。"""
    # --ciオプションの処理
    if args.ci:
        args.shuffle = False
        args.no_ui = True

    # --ui と --no-ui の競合チェック
    if args.ui and args.no_ui:
        parser.error("--ui と --no-ui は同時に指定できません。")

    # --version
    if args.version:
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    # --generate-config
    if args.generate_config:
        logger.info(pyfltr.config.generate_config_text())
        return 0

    # pyproject.toml
    try:
        config = pyfltr.config.load_config()
    except (ValueError, OSError) as e:
        logger.error(f"設定エラー: {e}")
        return 1

    # カスタムコマンド用のCLI引数を動的追加して再パース
    custom_commands = [name for name, info in config.commands.items() if not info.builtin]
    if custom_commands:
        for command in custom_commands:
            parser.add_argument(
                f"--{command}-args",
                default="",
                help=f"{command} への追加引数を指定します。",
            )
        args = parser.parse_args(sys_args)

    # --work-dir指定時、再パースで上書きされたtargetsを絶対パスで復元
    if resolved_targets is not None:
        args.targets = resolved_targets

    # CLIの--jobsオプションでconfigを上書き
    if args.jobs is not None:
        config.values["jobs"] = args.jobs

    commands: list[str] = pyfltr.config.resolve_aliases(args.commands.split(","), config)
    for command in commands:
        if command not in config.values:
            parser.error(f"コマンドが見つかりません: {command}")

    # fix モードの前処理
    if args.fix:
        if args.shuffle:
            # fix モードは修正の再現性を重視するためシャッフルを無効化
            logger.warning("--fix 指定時は --shuffle を無効化します。")
            args.shuffle = False
        commands = pyfltr.config.filter_fix_commands(commands, config)
        if not commands:
            logger.error(
                "--fix で実行可能なコマンドがありません"
                "(有効化された formatter もしくは fix-args 定義済み linter を指定してください)。"
            )
            return 1

    return run_pipeline(args, commands, config)


def run_pipeline(
    args: argparse.Namespace,
    commands: list[str],
    config: pyfltr.config.Config,
) -> int:
    """実行パイプライン。"""
    # ターミナルをクリア
    if not args.no_clear:
        subprocess.run("cls" if os.name == "nt" else "clear", check=False)

    # 実行環境の情報を出力
    logger.info(f"{'-' * 10} pyfltr {'-' * (72 - 10 - 8)}")
    logger.info(f"version:        {importlib.metadata.version('pyfltr')}")
    logger.info(f"sys.executable: {sys.executable}")
    logger.info(f"sys.version:    {sys.version}")
    logger.info(f"cwd:            {os.getcwd()}")
    logger.info("-" * 72)

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.ui.can_use_ui())

    # run
    if use_ui:
        results, returncode = pyfltr.ui.run_commands_with_ui(commands, args, config)
        # UI終了後に通常のログを出力
        for result in results:
            pyfltr.cli.write_log(result)
    else:
        results = pyfltr.cli.run_commands_with_cli(commands, args, config)
        returncode = 0

    # summary
    logger.info(f"{'-' * 10} summary {'-' * (72 - 10 - 9)}")
    for result in sorted(results, key=lambda r: config.command_names.index(r.command)):
        logger.info(f"    {result.command:<16s} {result.get_status_text()}")
    logger.info("-" * 72)

    # エラー箇所一覧
    all_errors: list[pyfltr.error_parser.ErrorLocation] = []
    for result in results:
        all_errors.extend(result.errors)
    if all_errors:
        sorted_errors = pyfltr.error_parser.sort_errors(all_errors, config.command_names)
        logger.info(f"{'-' * 10} errors ({len(sorted_errors)}) {'-' * (72 - 14 - len(str(len(sorted_errors))))}")
        for error in sorted_errors:
            logger.info(f"    {pyfltr.error_parser.format_error(error)}")
        logger.info("-" * 72)

    # returncode
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)
    return returncode


def calculate_returncode(results: list[pyfltr.command.CommandResult], exit_zero_even_if_formatted: bool) -> int:
    """終了コードを計算。"""
    statuses = [result.status for result in results]
    if any(status == "failed" for status in statuses):
        return 1
    if not exit_zero_even_if_formatted and any(status == "formatted" for status in statuses):
        return 1
    return 0


if __name__ == "__main__":
    main()
