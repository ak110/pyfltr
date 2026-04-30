#!/usr/bin/env python3
"""pyfltrエントリポイントとサブコマンドdispatch。

`main()` は console script エントリポイント。
`run()` は引数パースとサブコマンドdispatchを担う薄い関数。
実行系サブコマンドの本体は `cli/pipeline.py` の `_run_impl` / `run_pipeline` が担う。
"""

import argparse
import contextlib
import importlib.metadata
import logging
import os
import pathlib
import sys
import typing

import pyfltr.cli.command_info
import pyfltr.cli.config_subcmd
import pyfltr.cli.mcp_server
import pyfltr.cli.parser
import pyfltr.cli.pipeline
import pyfltr.cli.shell_completion
import pyfltr.command.env
import pyfltr.state.runs

logger = logging.getLogger(__name__)

# 環境変数を打ち消してリサイズに対応する
os.environ.pop("COLUMNS", None)
os.environ.pop("LINES", None)


def _reconfigure_stdio_to_utf8() -> None:
    """`sys.stdout` / `sys.stderr` をUTF-8で出力するよう切り替える。

    Windows + Python 3.14では `sys.stdout` / `sys.stderr` の既定エンコーディングが
    cp1252等のままになるケースがあり、`pyfltr.textout` が出す日本語ログで
    UnicodeEncodeErrorを起こす。`PYTHONUTF8` 環境変数や利用者側設定に依存せずに
    挙動を揃えるため、エントリポイント直後に `reconfigure` を試みる。
    `reconfigure` 未提供stream（差し替え済みTextIO等）や呼び出し失敗時は握り潰し、
    既存挙動を維持する。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(OSError, ValueError):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def main() -> typing.NoReturn:
    """エントリポイント。"""
    _reconfigure_stdio_to_utf8()
    # 親プロセスから継承したPATHの重複排除をプロセス全体で1回だけ適用する。
    # 以後 `os.environ` を継承する全subprocessに波及するため、個別箇所のenv
    # 構築では追加の重複排除を行わない。CLI経路でのみ呼ぶことで、ライブラリ
    # 利用時に意図せず別アプリの `os.environ` を書き換えないようにする。
    # 詳細はCLAUDE.md「subprocess起動時のPATH整理方針」節を参照。
    pyfltr.command.env.dedupe_environ_path(os.environ)
    exit_code = run()
    logger.debug(f"{exit_code=}")
    sys.exit(exit_code)


def run(sys_args: typing.Sequence[str] | None = None) -> int:
    """処理の実行。"""
    if sys_args is None:
        sys_args = sys.argv[1:]

    # -V / --versionはsubparser必須化の例外として、先頭に来た場合のみ短絡処理する。
    # argparseのrequired subparsersは位置引数を要求するため、単独の--versionでは
    # usageエラーになってしまう。`pyfltr -V` の利便性を維持するため明示的に捌く。
    if len(sys_args) == 1 and sys_args[0] in ("-V", "--version"):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    # ツール名をサブコマンドとして誤入力したケースを検知し、実行例付きで案内する。
    # argparse既定の "invalid choice" エラーより具体的な導線になるため先に捌く。
    pyfltr.cli.parser.preflight_tool_name_as_subcommand(sys_args)

    parser = pyfltr.cli.parser.build_parser()
    args = parser.parse_args(list(sys_args))
    subcommand = args.subcommand
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO, format="%(message)s")

    # 非実行系サブコマンドを辞書駆動でdispatchする。
    # 辞書値はcallable（lazy importでモジュールロードコストを実行時に先送り）。
    # 実行系（run/ci/fast/run-for-agent）は従来通り _run_impl へ委譲する。
    non_run_dispatch: dict[str, typing.Callable[[], int]] = {
        "config": lambda: pyfltr.cli.config_subcmd.execute(parser, args),
        "generate-shell-completion": lambda: _dispatch_shell_completion(args),
        "list-runs": lambda: pyfltr.state.runs.execute_list_runs(parser, args),
        "show-run": lambda: pyfltr.state.runs.execute_show_run(parser, args),
        "command-info": lambda: _dispatch_command_info(parser, args),
        "mcp": lambda: _dispatch_mcp(args),
    }

    if subcommand in non_run_dispatch:
        return non_run_dispatch[subcommand]()

    # サブコマンド別の既定値を注入する （CLI明示値が優先）。
    pyfltr.cli.parser.apply_subcommand_defaults(args)

    # --no-fix指定時はinclude_fix_stageをFalseに差し戻す。
    if getattr(args, "no_fix", False):
        args.include_fix_stage = False

    # retry_commandの対象ファイル差し替え時、--work-dir適用後の相対パスが
    # 実行時のcwdと二重解釈されないよう、常に元cwdを起点に絶対パス化する。
    # os.chdirよりも前のcwdを確実に取得するため、--work-dirの有無を問わず保存する。
    original_cwd = os.getcwd()
    resolved_targets: list[pathlib.Path] | None = None
    chdir_applied = False
    if args.work_dir is not None:
        resolved_targets = [t.absolute() for t in args.targets]
        os.chdir(args.work_dir)
        chdir_applied = True

    # カスタムコマンド用の再パースコールバック。cli/parserへの参照をここで保持することで
    # cli/pipeline→cli/parserの直接依存（循環importの原因）を回避する。
    def _reparse_with_custom(custom_commands: list[str]) -> tuple[argparse.ArgumentParser, argparse.Namespace]:
        p = pyfltr.cli.parser.build_parser(custom_commands)
        a = p.parse_args(list(sys_args))
        pyfltr.cli.parser.apply_subcommand_defaults(a)
        return p, a

    try:
        return pyfltr.cli.pipeline._run_impl(  # pylint: disable=protected-access
            parser,
            args,
            list(sys_args),
            resolved_targets,
            original_cwd=original_cwd,
            reparse_fn=_reparse_with_custom,
        )
    finally:
        if chdir_applied:
            os.chdir(original_cwd)


def _dispatch_shell_completion(args: argparse.Namespace) -> int:
    """generate-shell-completionサブコマンドの処理。"""
    # 補完スクリプト側は「サブコマンド + 共通オプション一式」を列挙する必要があるため、
    # 実行系サブコマンドの共通parentを渡す （カスタムコマンドは対象外で十分）。
    script = pyfltr.cli.shell_completion.generate(
        args.shell,
        pyfltr.cli.parser.make_common_parent(),
        frozenset(pyfltr.cli.parser._ALL_SUBCOMMANDS),  # pylint: disable=protected-access
    )
    print(script, end="")
    return 0


def _dispatch_command_info(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """command-infoサブコマンドの処理。"""
    return pyfltr.cli.command_info.execute_command_info(parser, args)


def _dispatch_mcp(args: argparse.Namespace) -> int:
    """mcpサブコマンドの処理。"""
    return pyfltr.cli.mcp_server.execute_mcp(args)


if __name__ == "__main__":
    main()
