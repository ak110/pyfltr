#!/usr/bin/env python3
"""pyfltr。"""

import argparse
import collections.abc
import importlib.metadata
import logging
import os
import pathlib
import subprocess
import sys
import typing

import pyfltr.archive
import pyfltr.cli
import pyfltr.command
import pyfltr.config
import pyfltr.llm_output
import pyfltr.shell_completion
import pyfltr.ui
import pyfltr.warnings_

logger = logging.getLogger(__name__)

# 環境変数を打ち消してリサイズに対応する
os.environ.pop("COLUMNS", None)
os.environ.pop("LINES", None)


# サブコマンド名とその挙動のマッピング。
# 実行系 (ci / run / fast / run-for-agent) は共通オプション (_COMMON_PARENT) を継承する。
# それ以外のサブコマンド (generate-config / generate-shell-completion) は固有の引数のみ持つ。
_RUN_SUBCOMMANDS: tuple[str, ...] = ("ci", "run", "fast", "run-for-agent")
"""実行系サブコマンド。パイプラインを起動して format/lint/test を走らせる。"""

_ALL_SUBCOMMANDS: tuple[str, ...] = (
    *_RUN_SUBCOMMANDS,
    "generate-config",
    "generate-shell-completion",
)
"""全サブコマンド。shell completion スクリプト生成時に参照される。"""


def main() -> typing.NoReturn:
    """エントリポイント。"""
    exit_code = run()
    logger.debug(f"{exit_code=}")
    sys.exit(exit_code)


def _make_common_parent(custom_commands: collections.abc.Iterable[str] = ()) -> argparse.ArgumentParser:
    """実行系サブコマンド用の共通オプションをまとめた親 parser を返す。

    ``parents=[common]`` 経由で各サブコマンドに継承させる。``custom_commands`` は
    ``pyproject.toml`` で定義されたカスタムコマンド名の列で、``--{cmd}-args``
    オプションとして追加登録される (ビルトインと同じ扱い)。
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", default=False, action="store_true", help="詳細な出力を表示します。")
    common.add_argument(
        "--exit-zero-even-if-formatted",
        default=False,
        action="store_true",
        help="linters/testers にエラーがある場合のみ exit 1 とします。",
    )
    common.add_argument(
        "--commands",
        default=None,
        help="カンマ区切りのコマンド一覧を指定します。"
        "(既定: ビルトイン + カスタムコマンドを含む、pyproject.toml で有効な全コマンド)",
    )
    common.add_argument("--ui", default=None, action="store_true", help="Textual UI を強制的に有効化します。")
    common.add_argument("--no-ui", default=None, action="store_true", help="Textual UI を強制的に無効化します。")
    common.add_argument(
        "--no-fix",
        default=False,
        action="store_true",
        help="run / fast / run-for-agent サブコマンドで自動付与される fix ステージを抑止します。",
    )
    common.add_argument(
        "--stream",
        default=False,
        action="store_true",
        help="各コマンドの完了時に即座に詳細ログを表示します (非 TUI モードでのみ有効)。"
        "既定ではすべてのコマンド完了後にまとめて表示します。",
    )
    common.add_argument("--shuffle", default=False, action="store_true", help="ファイル順をシャッフルします。")
    common.add_argument("--keep-ui", default=False, action="store_true", help="正常終了後も TUI を閉じずに維持します。")
    common.add_argument("--ci", default=False, action="store_true", help="CI モードで動作します(--no-shuffle --no-ui 相当)。")
    common.add_argument(
        "--output-format",
        choices=("text", "jsonl"),
        default=None,
        help="出力形式を指定します(text/jsonl、既定: text)。jsonl は LLM 向け JSON Lines 出力。"
        "未指定時は環境変数 PYFLTR_OUTPUT_FORMAT の値を使用します。",
    )
    common.add_argument(
        "--output-file",
        type=pathlib.Path,
        default=None,
        help="--output-format の出力先ファイル。未指定時は stdout に出力します。"
        "jsonl 併用時、ファイルには JSONL・stdout には従来の text 出力が並行して出ます。",
    )
    common.add_argument(
        "--human-readable",
        default=False,
        action="store_true",
        help="ツールの構造化出力（JSON等）を無効化し、人間向けの元のテキスト出力を使用します。",
    )
    common.add_argument("--no-clear", default=False, action="store_true", help="実行前にターミナルをクリアしません。")
    common.add_argument(
        "--no-exclude",
        default=False,
        action="store_true",
        help="exclude/extend-exclude パターンによるファイル除外を無効化します。",
    )
    common.add_argument(
        "--no-gitignore",
        default=False,
        action="store_true",
        help=".gitignore によるファイル除外を無効化します。",
    )
    common.add_argument(
        "--no-archive",
        default=False,
        action="store_true",
        help="実行アーカイブ (ユーザーキャッシュ配下への全実行の保存) を無効化します。",
    )
    common.add_argument(
        "--work-dir",
        type=pathlib.Path,
        default=None,
        help="実行前に作業ディレクトリを変更します(既定: カレントディレクトリ)。",
    )
    common.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=None,
        help="linters/testers の最大並列数を指定します(既定: 4、pyproject.toml でも設定可能です)。",
    )

    # 各コマンド用の引数追加オプション (ビルトイン + カスタム)
    registered: set[str] = set()
    for command in pyfltr.config.BUILTIN_COMMANDS:
        registered.add(command)
        common.add_argument(
            f"--{command}-args",
            default="",
            help=f"{command} への追加引数を指定します。",
        )
    for command in custom_commands:
        if command in registered:
            continue
        common.add_argument(
            f"--{command}-args",
            default="",
            help=f"{command} への追加引数を指定します。",
        )

    common.add_argument(
        "targets",
        nargs="*",
        type=pathlib.Path,
        help="対象のファイルまたはディレクトリを指定します(既定: カレントディレクトリ)。",
    )
    return common


def build_parser(custom_commands: collections.abc.Iterable[str] = ()) -> argparse.ArgumentParser:
    """引数パーサーを生成。サブコマンド必須化 (v3.0.0)。"""
    parser = argparse.ArgumentParser(
        epilog=(
            "サブコマンド:\n"
            "  ci               CI モードで実行する。フォーマッターの変更も失敗扱い。\n"
            "  run              通常実行。フォーマッターの変更は成功扱いで fix ステージ有効。\n"
            "  fast             高速ツールのみ実行 (--commands=fast 相当)。\n"
            "  run-for-agent    LLM エージェント向け (JSONL 出力を既定化)。\n"
            "  generate-config  pyproject.toml 用の設定雛形を出力する。\n"
            "  generate-shell-completion <shell>\n"
            "                   シェル補完スクリプトを出力する (bash / powershell)。\n"
            "\n"
            "ドキュメント: https://ak110.github.io/pyfltr/\n"
            "llms.txt: https://ak110.github.io/pyfltr/llms.txt"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="store_true", help="バージョンを表示します。")

    subparsers = parser.add_subparsers(dest="subcommand", required=True, metavar="<subcommand>")

    common = _make_common_parent(custom_commands)

    # サブコマンド別の既定値 (exit_zero_even_if_formatted / commands / output_format /
    # include_fix_stage) は ``_apply_subcommand_defaults`` で解決する。
    # subparser 単位の ``set_defaults`` は ``parents=[common]`` で共有された
    # 名前空間を通じて他サブパーサーの default も書き換えてしまうため採用しない
    # (argparse の既知挙動)。
    subparsers.add_parser("ci", parents=[common], help="CI モードで実行する。")
    subparsers.add_parser("run", parents=[common], help="通常実行。")
    subparsers.add_parser("fast", parents=[common], help="高速ツールのみ実行。")
    subparsers.add_parser("run-for-agent", parents=[common], help="LLM エージェント向け。")

    # generate-config: 設定雛形出力
    subparsers.add_parser("generate-config", help="pyproject.toml 用の設定雛形を出力する。")

    # generate-shell-completion: 補完スクリプト出力
    gsc_parser = subparsers.add_parser(
        "generate-shell-completion",
        help="シェル補完スクリプトを出力する。",
    )
    gsc_parser.add_argument(
        "shell",
        choices=pyfltr.shell_completion.SUPPORTED_SHELLS,
        help="出力するシェル種別。",
    )

    return parser


def _apply_subcommand_defaults(args: argparse.Namespace) -> None:
    """サブコマンドごとの既定値を ``args`` に反映する。

    ``subparsers.add_parser(..., parents=[common])`` で共通オプションを継承する
    構造上、``sub_parser.set_defaults(...)`` は他サブパーサーの default まで
    上書きしてしまうため (argparse の既知挙動)、argparse 本体の既定値機構は
    使わずここで手動解決する。CLI 明示値 (``store_true`` や値指定) は
    事前に args に載っているため、既定値注入は「未指定扱いの値」を上書きする
    形にとどめる。

    サブコマンド挙動:
        - ``ci``: fix ステージ無効。exit_zero_even_if_formatted は明示時のみ True
        - ``run``: fix ステージ有効。exit_zero_even_if_formatted を True に
        - ``fast``: run と同じ + ``--commands`` 未指定なら ``"fast"``
        - ``run-for-agent``: run と同じ + ``--output-format`` 未指定なら ``"jsonl"``
    """
    subcommand = args.subcommand
    args.include_fix_stage = subcommand in ("run", "fast", "run-for-agent")
    if subcommand in ("run", "fast", "run-for-agent"):
        args.exit_zero_even_if_formatted = True
    if subcommand == "fast" and args.commands is None:
        args.commands = "fast"
    if subcommand == "run-for-agent" and args.output_format is None:
        args.output_format = "jsonl"


def run(sys_args: typing.Sequence[str] | None = None) -> int:
    """処理の実行。"""
    if sys_args is None:
        sys_args = sys.argv[1:]

    # -V / --version は subparser 必須化の例外として、先頭に来た場合のみ短絡処理する。
    # argparse の required subparsers は位置引数を要求するため、単独の --version では
    # usage エラーになってしまう。`pyfltr -V` の利便性を維持するため明示的に捌く。
    if len(sys_args) == 1 and sys_args[0] in ("-V", "--version"):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    parser = build_parser()
    args = parser.parse_args(list(sys_args))
    subcommand = args.subcommand
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO, format="%(message)s")

    # generate-configサブコマンド: 他のオプションは無視して設定雛形を出力する
    if subcommand == "generate-config":
        logger.info(pyfltr.config.generate_config_text())
        return 0

    # generate-shell-completionサブコマンド: 補完スクリプトをstdoutに出力する
    if subcommand == "generate-shell-completion":
        # 補完スクリプト側は「サブコマンド + 共通オプション一式」を列挙する必要があるため、
        # 実行系サブコマンドの共通 parent を渡す (カスタムコマンドは対象外で十分)。
        script = pyfltr.shell_completion.generate(args.shell, _make_common_parent(), frozenset(_ALL_SUBCOMMANDS))
        print(script, end="")
        return 0

    # サブコマンド別の既定値を注入する (CLI 明示値が優先)。
    _apply_subcommand_defaults(args)

    # --no-fix 指定時は include_fix_stage を False に差し戻す。
    if getattr(args, "no_fix", False):
        args.include_fix_stage = False

    # --work-dir: ターゲットパスを絶対パスに変換してからcwd変更
    original_cwd: str | None = None
    resolved_targets: list[pathlib.Path] | None = None
    if args.work_dir is not None:
        resolved_targets = [t.absolute() for t in args.targets]
        original_cwd = os.getcwd()
        os.chdir(args.work_dir)
    try:
        return _run_impl(parser, args, list(sys_args), resolved_targets)
    finally:
        if original_cwd is not None:
            os.chdir(original_cwd)


_OUTPUT_FORMAT_ENV = "PYFLTR_OUTPUT_FORMAT"
_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset({"text", "jsonl"})


def _resolve_output_format(parser: argparse.ArgumentParser, cli_value: str | None) -> str:
    """CLI 引数 > 環境変数 > 既定値(text) の優先順で出力形式を決定する。

    環境変数に不正値が入っている場合は argparse 同様のエラーで即座に終了させる。
    """
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(_OUTPUT_FORMAT_ENV)
    if env_value is None or env_value == "":
        return "text"
    if env_value not in _VALID_OUTPUT_FORMATS:
        parser.error(
            f"環境変数 {_OUTPUT_FORMAT_ENV} に不正な値が指定されています: {env_value!r} "
            f"(有効値: {', '.join(sorted(_VALID_OUTPUT_FORMATS))})"
        )
    return env_value


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
    original_sys_args: typing.Sequence[str],
    resolved_targets: list[pathlib.Path] | None,
) -> int:
    """run()の内部実装 (実行系サブコマンド向け)。"""
    # 同一プロセス内で run() が複数回呼ばれるケースに備えて警告蓄積を初期化する。
    pyfltr.warnings_.clear()

    # --ciオプションの処理
    if args.ci:
        args.shuffle = False
        args.no_ui = True

    # --ui と --no-ui の競合チェック
    if args.ui and args.no_ui:
        parser.error("--ui と --no-ui は同時に指定できません。")

    # --version (実行系サブコマンド下でも許容)
    if args.version:
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    # jsonl stdout モード (CLI で `--output-format=jsonl` かつ `--output-file` 未指定) は
    # load_config() 前から判定できるため、先行して root logger を抑止する。これにより
    # load_config() 失敗時のエラーログや以降の警告/エラーが stdout/stderr に漏れない。
    # 抑止状態は try/finally で必ず復元する (run() は同一プロセス内で複数回呼ばれる設計)。
    output_format = _resolve_output_format(parser, args.output_format)
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
            parser_with_custom = build_parser(custom_commands)
            args = parser_with_custom.parse_args(list(original_sys_args))
            # 再パースで各種属性が初期化されるため、サブコマンド既定値とその他の確定値を再適用する。
            _apply_subcommand_defaults(args)
            args.output_format = output_format
            args.output_file = output_file
            if getattr(args, "no_fix", False):
                args.include_fix_stage = False
            if jsonl_stdout:
                _force_jsonl_stdout_mode(args)
            if args.ci:
                args.shuffle = False
                args.no_ui = True

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

    # 実行アーカイブの初期化 (既定で有効)。
    # ``--no-archive`` または ``archive = false`` で無効化できる。クリーンアップ失敗や
    # 書き込み失敗はパイプライン本体を止めないよう warnings へ流す。
    archive_enabled = bool(config.values.get("archive", True)) and not getattr(args, "no_archive", False)
    archive_store: pyfltr.archive.ArchiveStore | None = None
    run_id: str | None = None
    if archive_enabled:
        try:
            archive_store = pyfltr.archive.ArchiveStore()
            run_id = archive_store.start_run(commands=commands, files=len(all_files))
            removed = archive_store.cleanup(pyfltr.archive.policy_from_config(config))
            if removed:
                logger.debug("archive: 自動削除で %d 件の古い run を削除", len(removed))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="archive", message=f"実行アーカイブを初期化できません: {e}")
            archive_store = None
            run_id = None

    archive_hook: typing.Callable[[pyfltr.command.CommandResult], None] | None = None
    if archive_store is not None and run_id is not None:
        captured_store = archive_store
        captured_run_id = run_id

        def _archive_hook(result: pyfltr.command.CommandResult) -> None:
            try:
                captured_store.write_tool_result(captured_run_id, result)
            except OSError as e:
                # ハンドラ内で warning を出しても summary 末尾にまとまる。
                pyfltr.warnings_.emit_warning(source="archive", message=f"{result.command} のアーカイブ書き込みに失敗: {e}")

        archive_hook = _archive_hook

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.ui.can_use_ui())

    # JSONL stdoutモード: ツール完了時に随時JSONL行を書き出す
    jsonl_stdout = (args.output_format == "jsonl") and (args.output_file is None)

    if jsonl_stdout:
        pyfltr.llm_output.write_jsonl_header(commands=commands, files=len(all_files), run_id=run_id)

    # run
    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    if use_ui:
        results, returncode = pyfltr.ui.run_commands_with_ui(commands, args, config, all_files, archive_hook=archive_hook)
        include_details = True
    else:
        # 非 TUI モード: 既定はバッファリング (最後にまとめて出力)、`--stream` で従来の即時出力。
        per_command_log = bool(args.stream)
        on_result = (lambda result: pyfltr.llm_output.write_jsonl_streaming(result, config)) if jsonl_stdout else None
        results = pyfltr.cli.run_commands_with_cli(
            commands,
            args,
            config,
            all_files,
            per_command_log=per_command_log,
            include_fix_stage=include_fix_stage,
            on_result=on_result,
            archive_hook=archive_hook,
        )
        returncode = 0
        # `--stream` のときは詳細ログは既に出力済み。summary のみ表示する。
        include_details = not per_command_log

    # returncode を先に確定させる (render_results に渡して JSONL summary.exit に埋めるため)
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)

    if jsonl_stdout:
        # ストリーミングモード: diagnostic行+tool行は出力済み。footer（warning+summary）のみ書き出す
        pyfltr.llm_output.write_jsonl_footer(
            results,
            exit_code=returncode,
            warnings=pyfltr.warnings_.collected_warnings(),
        )
    else:
        pyfltr.cli.render_results(
            results,
            config,
            include_details=include_details,
            output_format=args.output_format or "text",
            output_file=args.output_file,
            exit_code=returncode,
            commands=commands,
            files=len(all_files),
            warnings=pyfltr.warnings_.collected_warnings(),
            run_id=run_id,
        )

    # アーカイブ終端: meta.json に exit_code / finished_at を書き込む。
    if archive_store is not None and run_id is not None:
        try:
            archive_store.finalize_run(run_id, exit_code=returncode, commands=commands, files=len(all_files))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="archive", message=f"meta.json の更新に失敗: {e}")

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
