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
import pyfltr.ui
import pyfltr.warnings_

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
    parser.add_argument("-v", "--verbose", default=False, action="store_true", help="詳細な出力を表示します。")
    parser.add_argument(
        "--exit-zero-even-if-formatted",
        default=False,
        action="store_true",
        help="linters/testers にエラーがある場合のみ exit 1 とします。",
    )
    parser.add_argument(
        "--commands",
        default=None,
        help="カンマ区切りのコマンド一覧を指定します。"
        "(既定: ビルトイン + カスタムコマンドを含む、pyproject.toml で有効な全コマンド)",
    )
    parser.add_argument("--ui", default=None, action="store_true", help="Textual UI を強制的に有効化します。")
    parser.add_argument("--no-ui", default=None, action="store_true", help="Textual UI を強制的に無効化します。")
    parser.add_argument(
        "--no-fix",
        default=False,
        action="store_true",
        help="run / fast サブコマンドで自動付与される fix ステージを抑止します。",
    )
    parser.add_argument(
        "--stream",
        default=False,
        action="store_true",
        help="各コマンドの完了時に即座に詳細ログを表示します (非 TUI モードでのみ有効)。"
        "既定ではすべてのコマンド完了後にまとめて表示します。",
    )
    parser.add_argument("--shuffle", default=False, action="store_true", help="ファイル順をシャッフルします。")
    parser.add_argument("--keep-ui", default=False, action="store_true", help="正常終了後も TUI を閉じずに維持します。")
    parser.add_argument("--ci", default=False, action="store_true", help="CI モードで動作します(--no-shuffle --no-ui 相当)。")
    parser.add_argument(
        "--output-format",
        choices=("text", "jsonl"),
        default=None,
        help="出力形式を指定します(text/jsonl、既定: text)。jsonl は LLM 向け JSON Lines 出力。",
    )
    parser.add_argument(
        "--output-file",
        type=pathlib.Path,
        default=None,
        help="--output-format の出力先ファイル。未指定時は stdout に出力します。"
        "jsonl 併用時、ファイルには JSONL・stdout には従来の text 出力が並行して出ます。",
    )
    parser.add_argument(
        "--human-readable",
        default=False,
        action="store_true",
        help="ツールの構造化出力（JSON等）を無効化し、人間向けの元のテキスト出力を使用します。",
    )
    parser.add_argument("--no-clear", default=False, action="store_true", help="実行前にターミナルをクリアしません。")
    parser.add_argument(
        "--no-exclude",
        default=False,
        action="store_true",
        help="exclude/extend-exclude パターンによるファイル除外を無効化します。",
    )
    parser.add_argument(
        "--no-gitignore",
        default=False,
        action="store_true",
        help=".gitignore によるファイル除外を無効化します。",
    )
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
    parser.add_argument("-V", "--version", action="store_true", help="バージョンを表示します。")
    return parser


# サブコマンドとして認識する予約語
_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "ci",
        "run",
        "fast",
        "generate-config",
        # 以下は廃止済み
        "fix",
        "dirty",
    }
)


def _parse_subcommand(sys_args: typing.Sequence[str]) -> tuple[str, list[str]]:
    """第一引数からサブコマンドを判定し、(subcommand, remaining_args)を返す。

    第一引数が予約済みサブコマンド名でなければ "ci" として扱う（後方互換性維持）。
    """
    if sys_args and sys_args[0] in _SUBCOMMANDS:
        return sys_args[0], list(sys_args[1:])
    return "ci", list(sys_args)


def _build_effective_args(subcommand: str, args: list[str]) -> list[str]:
    """サブコマンドに応じた暗黙的オプションを先頭に挿入。"""
    if subcommand == "run":
        return ["--exit-zero-even-if-formatted", *args]
    if subcommand == "fast":
        return ["--exit-zero-even-if-formatted", "--commands=fast", *args]
    # ci: 変更なし
    return list(args)


def run(sys_args: typing.Sequence[str] | None = None) -> int:
    """処理の実行。"""
    if sys_args is None:
        sys_args = sys.argv[1:]

    subcommand, remaining_args = _parse_subcommand(sys_args)

    # 廃止済みサブコマンド
    if subcommand in ("fix", "dirty"):
        logger.error(f"{subcommand} サブコマンドは廃止されました。")
        return 1

    # generate-configサブコマンド: 他のオプションは無視して設定雛形を出力する
    if subcommand == "generate-config":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logger.info(pyfltr.config.generate_config_text())
        return 0

    effective_args = _build_effective_args(subcommand, remaining_args)

    parser = build_parser()
    args = parser.parse_args(effective_args)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")

    # fix ステージは run / fast で既定有効、ci では無効。`--no-fix` で明示抑止も可。
    args.include_fix_stage = subcommand in ("run", "fast") and not args.no_fix

    # --work-dir: ターゲットパスを絶対パスに変換してからcwd変更
    original_cwd: str | None = None
    resolved_targets: list[pathlib.Path] | None = None
    if args.work_dir is not None:
        resolved_targets = [t.absolute() for t in args.targets]
        original_cwd = os.getcwd()
        os.chdir(args.work_dir)
    try:
        return _run_impl(parser, args, effective_args, resolved_targets)
    finally:
        if original_cwd is not None:
            os.chdir(original_cwd)


def _force_jsonl_stdout_mode(args: argparse.Namespace) -> None:
    """Jsonl + stdout モード時、UI/進捗系オプションを silently 無効化する。

    `parser.error()` で拒否すると argparse が usage を stderr に書くため
    「stdout/stderr とも完全に抑止」の要件に反する。既存 `--ci` が
    `args.no_ui=True` を silently 強制する先例に揃えて無音で上書きする。
    """
    args.ui = None
    args.no_ui = True
    args.no_clear = True
    args.stream = False


def _suppress_logging() -> tuple[list[logging.Handler], int]:
    """Root logger の handlers と level を保存しつつ完全抑止する。

    復元値をタプルで返す。復元は `_restore_logging()` で行う。`run()` は
    同一プロセスで複数回呼ばれる設計のため、必ず呼び出し側で `try`/`finally`
    による復元を保証すること。
    """
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    root.handlers.clear()
    root.setLevel(logging.CRITICAL + 1)
    return saved


def _restore_logging(saved: tuple[list[logging.Handler], int]) -> None:
    """`_suppress_logging()` の戻り値から root logger 状態を復元する。"""
    handlers, level = saved
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)


def _run_impl(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    effective_args: typing.Sequence[str],
    resolved_targets: list[pathlib.Path] | None,
) -> int:
    """run()の内部実装。"""
    # 同一プロセス内で run() が複数回呼ばれるケースに備えて警告蓄積を初期化する。
    pyfltr.warnings_.clear()

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

    # jsonl stdout モード (CLI で `--output-format=jsonl` かつ `--output-file` 未指定) は
    # load_config() 前から判定できるため、先行して root logger を抑止する。これにより
    # load_config() 失敗時のエラーログや以降の警告/エラーが stdout/stderr に漏れない。
    # 抑止状態は try/finally で必ず復元する (run() は同一プロセス内で複数回呼ばれる設計)。
    output_format: str = args.output_format or "text"
    output_file: pathlib.Path | None = args.output_file
    jsonl_stdout = output_format == "jsonl" and output_file is None
    if jsonl_stdout:
        _force_jsonl_stdout_mode(args)
    suppression = _suppress_logging() if jsonl_stdout else None

    try:
        # pyproject.toml
        try:
            config = pyfltr.config.load_config()
        except (ValueError, OSError) as e:
            if jsonl_stdout:
                # 抑止済みなので text 出力は出せない。LLM 側は JSONL 0 行 + exit 非 0 で検知する。
                return 1
            logger.error(f"設定エラー: {e}")
            return 1

        args.output_format = output_format
        args.output_file = output_file

        # カスタムコマンド用のCLI引数を動的追加して再パース
        custom_commands = [name for name, info in config.commands.items() if not info.builtin]
        if custom_commands:
            for command in custom_commands:
                parser.add_argument(
                    f"--{command}-args",
                    default="",
                    help=f"{command} への追加引数を指定します。",
                )
            args = parser.parse_args(effective_args)
            # 再パースで output-format/output-file が元に戻るため、確定値を再適用する
            args.output_format = output_format
            args.output_file = output_file
            if jsonl_stdout:
                _force_jsonl_stdout_mode(args)

        # --work-dir指定時、再パースで上書きされたtargetsを絶対パスで復元
        if resolved_targets is not None:
            args.targets = resolved_targets

        # CLIオプションでconfigを上書き
        if args.jobs is not None:
            config.values["jobs"] = args.jobs
        if args.no_exclude:
            config.values["exclude"] = []
            config.values["extend-exclude"] = []
        if args.no_gitignore:
            config.values["respect-gitignore"] = False
        if args.human_readable:
            for key in list(config.values):
                if key.endswith("-json") or key == "pytest-tb-line":
                    config.values[key] = False

        # --commands 未指定時はカスタムコマンドを含む全登録コマンドを対象にする。
        # argparse のデフォルト評価時点では pyproject.toml を読み込んでいないため、
        # ビルトインのみの default を返すと custom-commands が常にスキップされる。
        # load_config 後に実体を決定することで、ユーザーが登録した custom-commands
        # (例: svelte-check) も `run` / `ci` サブコマンドのデフォルト動作で走るようにする。
        commands_arg: str = args.commands if args.commands is not None else ",".join(config.command_names)
        commands: list[str] = pyfltr.config.resolve_aliases(commands_arg.split(","), config)
        for command in commands:
            if command not in config.values:
                parser.error(f"コマンドが見つかりません: {command}")

        return run_pipeline(args, commands, config)
    finally:
        if suppression is not None:
            _restore_logging(suppression)


def run_pipeline(
    args: argparse.Namespace,
    commands: list[str],
    config: pyfltr.config.Config,
) -> int:
    """実行パイプライン。"""
    # ターミナルをクリア
    if not args.no_clear:
        subprocess.run("cls" if os.name == "nt" else "clear", check=False, shell=True)

    # 実行環境の情報を出力
    logger.info(f"{'-' * 10} pyfltr {'-' * (72 - 10 - 8)}")
    logger.info(f"version:        {importlib.metadata.version('pyfltr')}")
    logger.info(f"sys.executable: {sys.executable}")
    logger.info(f"sys.version:    {sys.version}")
    logger.info(f"cwd:            {os.getcwd()}")
    logger.info("-" * 72)

    # 対象ファイルを一括展開（ディレクトリ走査・exclude・gitignoreフィルタリングを1回だけ実行）
    # TUI起動前に実行することで、除外警告がログに表示される
    all_files = pyfltr.command.expand_all_files(args.targets, config)

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.ui.can_use_ui())

    # run
    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    if use_ui:
        results, returncode = pyfltr.ui.run_commands_with_ui(commands, args, config, all_files)
        include_details = True
    else:
        # 非 TUI モード: 既定はバッファリング (最後にまとめて出力)、`--stream` で従来の即時出力。
        per_command_log = bool(args.stream)
        results = pyfltr.cli.run_commands_with_cli(
            commands, args, config, all_files, per_command_log=per_command_log, include_fix_stage=include_fix_stage
        )
        returncode = 0
        # `--stream` のときは詳細ログは既に出力済み。summary のみ表示する。
        include_details = not per_command_log

    # returncode を先に確定させる (render_results に渡して JSONL summary.exit に埋めるため)
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)

    pyfltr.cli.render_results(
        results,
        config,
        include_details=include_details,
        output_format=args.output_format or "text",
        output_file=args.output_file,
        exit_code=returncode,
        warnings=pyfltr.warnings_.collected_warnings(),
    )
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
