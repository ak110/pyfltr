#!/usr/bin/env python3
"""pyfltr。"""

# v3.0.0 でサブコマンド・実行アーカイブ・キャッシュ・retry_command 絞り込みなどを
# 段階的に集約した。retry_command 生成は pyfltr.retry モジュールが担う。

import argparse
import collections.abc
import importlib.metadata
import logging
import os
import pathlib
import shlex
import subprocess
import sys
import typing

import pyfltr.archive
import pyfltr.cache
import pyfltr.cli
import pyfltr.command
import pyfltr.config
import pyfltr.formatters
import pyfltr.mcp_
import pyfltr.only_failed
import pyfltr.precommit
import pyfltr.retry
import pyfltr.runs
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
    "list-runs",
    "show-run",
    "mcp",
)
"""全サブコマンド。shell completion スクリプト生成時に参照される。"""


_STATIC_COMMAND_ALIASES: tuple[str, ...] = ("format", "lint", "test")
"""組み込みで必ず定義されるコマンドエイリアス。ユーザー設定のカスタムエイリアスは含まない。

ツール名プリフライト（個別ツール絞り込み誘導）の検出集合に含める。
カスタムコマンド・カスタムエイリアスは pyproject.toml 読込後にしか確定しないため
当面プリフライト対象外とする。
"""


class _HelpOnErrorArgumentParser(argparse.ArgumentParser):
    """argparse エラー時に `--help` 相当を stderr に併記してから終了する ArgumentParser。

    argparse 既定の error() はエラー文のみを出して exit 2 するため、利用者が正しい書式を
    取り違えたまま同じミスを繰り返しやすい。本サブクラスではエラー文の前に
    ``self.print_help(sys.stderr)`` を呼び、該当 parser のヘルプを併記する。
    サブコマンド側のエラーでは当該サブコマンドの parser、メインの誤サブコマンドでは
    メインの parser のヘルプが出る（argparse の階層別 parser_class で継承させるため）。
    """

    def error(self, message: str) -> typing.NoReturn:
        self.print_help(sys.stderr)
        self.exit(2, f"\n{self.prog}: error: {message}\n")


def _preflight_tool_name_as_subcommand(sys_args: typing.Sequence[str]) -> None:
    """ツール名をサブコマンドとして入力したケースを検知し、実行例付きメッセージを出して exit 2。

    `uv run pyfltr textlint ...` / `pyfltr lint docs/` など、利用者がツール名またはエイリアスを
    そのままサブコマンドとして指定した場合に、正しい `--commands=<tool>` 書式の実行例を提示する。
    該当しない場合は何も行わずに返し、通常の argparse 処理を続行する。

    検出対象は ``pyfltr.builtin_commands.BUILTIN_COMMAND_NAMES`` + 静的エイリアス
    ``format`` / ``lint`` / ``test``。カスタムコマンドは pyproject.toml 読込後にしか
    確定しないため対象外とする。
    """
    if not sys_args:
        return
    candidate = sys_args[0]
    if candidate in _ALL_SUBCOMMANDS:
        return
    tool_names = frozenset(pyfltr.config.BUILTIN_COMMAND_NAMES) | frozenset(_STATIC_COMMAND_ALIASES)
    if candidate not in tool_names:
        return
    rest_args = " ".join(shlex.quote(a) for a in sys_args[1:]) if len(sys_args) > 1 else "[targets]"
    message = (
        f"{candidate!r} はツール名またはエイリアスであり、pyfltr のサブコマンドとしては受け付けられません。\n"
        f"個別ツールを実行する場合は --commands オプションを使ってください。\n"
        f"\n"
        f"実行例:\n"
        f"  pyfltr run --commands={candidate} {rest_args}\n"
        f"  pyfltr run-for-agent --commands={candidate} {rest_args}\n"
        f"\n"
        f"失敗ファイルのみを再実行する tool.retry_command も既に `--commands=<tool>` 書式で出力されます。\n"
    )
    print(message, file=sys.stderr, end="")
    sys.exit(2)


def main() -> typing.NoReturn:
    """エントリポイント。"""
    exit_code = run()
    logger.debug(f"{exit_code=}")
    sys.exit(exit_code)


def _make_common_parent(custom_commands: collections.abc.Iterable[str] = ()) -> "_HelpOnErrorArgumentParser":
    """実行系サブコマンド用の共通オプションをまとめた親 parser を返す。

    ``parents=[common]`` 経由で各サブコマンドに継承させる。``custom_commands`` は
    ``pyproject.toml`` で定義されたカスタムコマンド名の列で、``--{cmd}-args``
    オプションとして追加登録される (ビルトインと同じ扱い)。
    """
    common = _HelpOnErrorArgumentParser(add_help=False)
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
        choices=sorted(pyfltr.formatters.FORMATTERS.keys()),
        default=None,
        help="出力形式を指定します(text/jsonl/sarif/github-annotations/code-quality、既定: text)。"
        "jsonl は LLM 向け JSON Lines 出力、sarif は SARIF 2.1.0、github-annotations は GitHub Actions 向けの注釈形式、"
        "code-quality は GitLab CI の artifacts:reports:codequality 向けの Code Climate JSON issue 形式。"
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
        "--no-cache",
        default=False,
        action="store_true",
        help="ファイル hash キャッシュ (対象ファイル未変更時の再実行スキップ) を無効化します。",
    )
    common.add_argument(
        "--fail-fast",
        default=False,
        action="store_true",
        help="1 ツールでもエラーが発生した時点で残りのジョブを打ち切ります。"
        "起動済みサブプロセスには terminate() を送り、未開始ジョブは skipped として扱われます。",
    )
    common.add_argument(
        "--only-failed",
        default=False,
        action="store_true",
        help="直前 run のアーカイブから失敗ツールと失敗ファイルを抽出し、"
        "ツール別に失敗ファイル集合のみを対象として再実行します。"
        "直前 run が存在しない/失敗ツールが無い場合はメッセージを出して成功終了します。",
    )
    common.add_argument(
        "--from-run",
        default=None,
        metavar="RUN_ID",
        help="--only-failed の参照対象 run を明示指定します（前方一致 / latest 対応）。未指定時は直前 run を自動選択します。",
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


def build_parser(custom_commands: collections.abc.Iterable[str] = ()) -> "_HelpOnErrorArgumentParser":
    """引数パーサーを生成。サブコマンド必須化 (v3.0.0)。

    メインおよび全サブコマンド parser には ``_HelpOnErrorArgumentParser`` を用い、
    argparse エラー時に該当 parser の ``--help`` 相当をまとめて stderr へ出す。
    """
    parser = _HelpOnErrorArgumentParser(
        epilog=(
            "サブコマンド:\n"
            "  ci               CI モードで実行する。フォーマッターの変更も失敗扱い。\n"
            "  run              通常実行。フォーマッターの変更は成功扱いで fix ステージ有効。\n"
            "  fast             高速ツールのみ実行 (--commands=fast 相当)。\n"
            "  run-for-agent    LLM エージェント向け (JSONL 出力を既定化)。\n"
            "  generate-config  pyproject.toml 用の設定雛形を出力する。\n"
            "  generate-shell-completion <shell>\n"
            "                   シェル補完スクリプトを出力する (bash / powershell)。\n"
            "  list-runs        実行アーカイブ内の run 一覧を表示する。\n"
            "  show-run <run_id>\n"
            "                   指定 run の詳細 (meta・ツール別サマリ・diagnostic・生出力) を表示する。\n"
            "  mcp              MCP サーバーを stdio で起動する。\n"
            "\n"
            "ドキュメント: https://ak110.github.io/pyfltr/\n"
            "llms.txt: https://ak110.github.io/pyfltr/llms.txt"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="store_true", help="バージョンを表示します。")

    subparsers = parser.add_subparsers(
        dest="subcommand",
        required=True,
        metavar="<subcommand>",
        parser_class=_HelpOnErrorArgumentParser,
    )

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

    # list-runs / show-run: 実行アーカイブの詳細参照サブコマンド
    pyfltr.runs.register_subparsers(subparsers)

    # mcp: MCP サーバーの stdio 起動
    pyfltr.mcp_.register_subparsers(subparsers)

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

    # ツール名をサブコマンドとして誤入力したケースを検知し、実行例付きで案内する。
    # argparse 既定の "invalid choice" エラーより具体的な導線になるため先に捌く。
    _preflight_tool_name_as_subcommand(sys_args)

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

    # 実行アーカイブの詳細参照サブコマンド: load_config() を呼ばず archive のみを参照する。
    if subcommand == "list-runs":
        return pyfltr.runs.execute_list_runs(args)
    if subcommand == "show-run":
        return pyfltr.runs.execute_show_run(args)

    # MCP サーバーサブコマンド: stdio で FastMCP サーバーを起動する。
    if subcommand == "mcp":
        return pyfltr.mcp_.execute_mcp(args)

    # サブコマンド別の既定値を注入する (CLI 明示値が優先)。
    _apply_subcommand_defaults(args)

    # --no-fix 指定時は include_fix_stage を False に差し戻す。
    if getattr(args, "no_fix", False):
        args.include_fix_stage = False

    # retry_command の対象ファイル差し替え時、--work-dir 適用後の相対パスが
    # 実行時の cwd と二重解釈されないよう、常に元 cwd を起点に絶対パス化する。
    # os.chdir よりも前の cwd を確実に取得するため、--work-dir の有無を問わず保存する。
    original_cwd = os.getcwd()
    resolved_targets: list[pathlib.Path] | None = None
    chdir_applied = False
    if args.work_dir is not None:
        resolved_targets = [t.absolute() for t in args.targets]
        os.chdir(args.work_dir)
        chdir_applied = True
    try:
        return _run_impl(parser, args, list(sys_args), resolved_targets, original_cwd=original_cwd)
    finally:
        if chdir_applied:
            os.chdir(original_cwd)


_OUTPUT_FORMAT_ENV = "PYFLTR_OUTPUT_FORMAT"
_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(pyfltr.formatters.FORMATTERS.keys())


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


def _run_impl(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    original_sys_args: typing.Sequence[str],
    resolved_targets: list[pathlib.Path] | None,
    *,
    original_cwd: str,
) -> int:
    """run()の内部実装 (実行系サブコマンド向け)。"""
    # 同一プロセス内で run() が複数回呼ばれるケースに備えて警告蓄積を初期化する。
    pyfltr.warnings_.clear()

    # --ciオプションの処理
    if args.ci:
        args.shuffle = False
        args.no_ui = True

    # --from-run は --only-failed との併用が必須
    if getattr(args, "from_run", None) is not None and not getattr(args, "only_failed", False):
        parser.error("argument --from-run: requires --only-failed")

    # --ui と --no-ui の競合チェック
    if args.ui and args.no_ui:
        parser.error("--ui と --no-ui は同時に指定できません。")

    # --version (実行系サブコマンド下でも許容)
    if args.version:
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    output_format = _resolve_output_format(parser, args.output_format)
    output_file: pathlib.Path | None = args.output_file

    # pyproject.toml
    try:
        config = pyfltr.config.load_config()
    except (ValueError, OSError) as e:
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

    exit_code, _run_id = run_pipeline(
        args, commands, config, original_cwd=original_cwd, original_sys_args=list(original_sys_args)
    )
    return exit_code


def run_pipeline(
    args: argparse.Namespace,
    commands: list[str],
    config: pyfltr.config.Config,
    *,
    original_cwd: str | None = None,
    original_sys_args: list[str] | None = None,
    force_text_on_stderr: bool = False,
) -> tuple[int, str | None]:
    """実行パイプライン。

    ``force_text_on_stderr=True`` を渡すと、人間向け text 整形ログの出力先を
    stdout ではなく stderr に強制する（MCP 経路で stdout を JSON-RPC フレームが
    占有するケース用）。

    Returns:
        ``(exit_code, run_id)`` のタプル。
        ``exit_code`` は 0 = 成功、1 = 失敗。
        ``run_id`` は実行アーカイブが有効で採番に成功した場合の ULID 文字列、
        無効または採番失敗時は ``None``。
    """
    output_format = args.output_format or "text"
    output_file: pathlib.Path | None = args.output_file
    # JSONL / SARIF / code-quality の stdout モードでは stdout を構造化出力が占有するため、
    # UI・画面クリア・stream による詳細ログ即時出力を無効化する。
    structured_stdout = output_format in ("jsonl", "sarif", "code-quality") and output_file is None
    if structured_stdout:
        args.ui = None
        args.no_ui = True
        args.no_clear = True
        args.stream = False

    formatter = pyfltr.formatters.FORMATTERS[output_format]()

    # logger を初期化する。同一プロセスで run_pipeline が複数回呼ばれる MCP 経路でも、
    # format / output_file / force_text_on_stderr の組み合わせで出力先が切り替わるため、毎回張り直す。
    # configure_loggers は output_file / force_text_on_stderr のみ参照するため、
    # run_id 等が未確定の段階でも呼び出せる（残フィールドはデフォルト値のまま渡す）。
    early_ctx = pyfltr.formatters.RunOutputContext(
        config=config,
        output_file=output_file,
        force_text_on_stderr=force_text_on_stderr,
    )
    formatter.configure_loggers(early_ctx)

    # ターミナルをクリア
    if not args.no_clear:
        clear_cmd = ["cmd", "/c", "cls"] if os.name == "nt" else ["clear"]
        subprocess.run(clear_cmd, check=False)

    # 対象ファイルを一括展開（ディレクトリ走査・exclude・gitignoreフィルタリングを1回だけ実行）
    # TUI起動前に実行することで、除外警告がログに表示される
    all_files = pyfltr.command.expand_all_files(args.targets, config)

    # --only-failed 指定時は直前 run からツール別の失敗ファイル集合を構築する。
    # archive / cache 初期化より前に実行し、早期終了の場合はそれらの副作用を発生させない。
    commands, only_failed_targets, only_failed_exit_early = pyfltr.only_failed.apply_filter(
        args, commands, all_files, from_run=getattr(args, "from_run", None)
    )
    if only_failed_exit_early:
        return 0, None

    # retry_command 再構成用のベース情報を確定する。original_cwd は run() が保存した
    # --work-dir 適用前の cwd、original_sys_args は起動時の sys.argv[1:] のコピー。
    effective_cwd = original_cwd if original_cwd is not None else os.getcwd()
    effective_sys_args = list(original_sys_args) if original_sys_args is not None else list(sys.argv[1:])
    launcher_prefix = pyfltr.retry.detect_launcher_prefix()
    retry_args_template = pyfltr.retry.build_retry_args_template(effective_sys_args)

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

    # 実行環境の情報を出力（run_id 採番後にまとめて出すことで区切り線内に含める）。
    pyfltr.cli.text_logger.info(f"{'-' * 10} pyfltr {'-' * (72 - 10 - 8)}")
    pyfltr.cli.text_logger.info(f"version:        {importlib.metadata.version('pyfltr')}")
    pyfltr.cli.text_logger.info(f"sys.executable: {sys.executable}")
    pyfltr.cli.text_logger.info(f"sys.version:    {sys.version}")
    pyfltr.cli.text_logger.info(f"cwd:            {os.getcwd()}")
    if run_id is not None:
        launcher_cmd = shlex.join(launcher_prefix)
        pyfltr.cli.text_logger.info("run_id:         %s（`%s show-run %s` で詳細を確認可能）", run_id, launcher_cmd, run_id)
    pyfltr.cli.text_logger.info("-" * 72)

    # ファイル hash キャッシュの初期化 (既定で有効)。
    # ``--no-cache`` または ``cache = false`` で無効化できる。期間超過エントリの削除失敗や
    # 書き込み失敗はパイプライン本体を止めないため warnings に流す。
    cache_enabled = bool(config.values.get("cache", True)) and not getattr(args, "no_cache", False)
    cache_store: pyfltr.cache.CacheStore | None = None
    if cache_enabled:
        try:
            cache_store = pyfltr.cache.CacheStore()
            cache_removed = cache_store.cleanup(pyfltr.cache.cache_policy_from_config(config))
            if cache_removed:
                logger.debug("cache: 期間超過で %d 件のエントリを削除", len(cache_removed))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="cache", message=f"ファイル hash キャッシュを初期化できません: {e}")
            cache_store = None

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
                return
            # 書き込み成功時のみ archived=True に更新。smart truncation の可否判定に使う。
            result.archived = True

        archive_hook = _archive_hook

    # retry_command を CommandResult に埋めるためのヘルパー。
    # archive_hook と同じタイミング (各ツール完了時) に呼ばれる on_result 経路へ挿入する。
    # 実装本体は ``_populate_retry_command`` (A案の失敗ファイル絞り込み・cached
    # 判定を含む) に委譲し、クロージャ変数をキーワード引数で引き渡す。
    def _attach_retry_command(result: pyfltr.command.CommandResult) -> None:
        pyfltr.retry.populate_retry_command(
            result,
            retry_args_template=retry_args_template,
            launcher_prefix=launcher_prefix,
            original_cwd=effective_cwd,
        )

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.ui.can_use_ui())

    # run_pipeline が1回だけ組み立てる不変コンテキスト。
    # archive_store は hook 経由で渡すため Context には含めない。
    base_ctx = pyfltr.command.ExecutionBaseContext(
        config=config,
        all_files=all_files,
        cache_store=cache_store,
        cache_run_id=run_id,
    )

    # 各ツール完了時のフック: retry_command 付与 → archive 書き込み → formatter.on_result (ストリーミング等)。
    # retry_command は archive と JSONL streaming の双方で必要になるため、archive_hook より前に挿入する。
    # formatter.on_result は archive_hook の後に呼ぶ（result.archived=True が立った後）。
    # on_start / on_result / on_finish で使う完全な ctx を構築する。
    per_command_log = bool(args.stream)
    include_details_from_stream = not per_command_log
    ctx = pyfltr.formatters.RunOutputContext(
        config=config,
        output_file=output_file,
        force_text_on_stderr=force_text_on_stderr,
        commands=commands,
        all_files=len(all_files),
        run_id=run_id,
        launcher_prefix=launcher_prefix,
        retry_args_template=retry_args_template,
        stream=per_command_log,
        include_details=include_details_from_stream,
        structured_stdout=structured_stdout,
    )

    formatter.on_start(ctx)

    # 各ツール完了時のフック順序:
    #   1. _attach_retry_command(result) → retry_command を result に付与
    #   2. archive_hook(result) → アーカイブ書き込み（cached の場合はスキップ）
    #   3. formatter.on_result(ctx, result) → JSONL streaming など（cached でも呼ばれる）
    # 上記 1+2 を composed_hook にまとめ、3 は run_commands_with_cli の on_result 引数として渡す。
    # これにより cached の場合でも formatter.on_result が呼ばれる（cli.py の設計を踏襲）。
    composed_hook: typing.Callable[[pyfltr.command.CommandResult], None] | None = None
    if archive_hook is not None:

        def _composed_archive_hook(result: pyfltr.command.CommandResult) -> None:
            _attach_retry_command(result)
            archive_hook(result)

        composed_hook = _composed_archive_hook
    else:
        composed_hook = _attach_retry_command

    def _on_result_callback(result: pyfltr.command.CommandResult) -> None:
        formatter.on_result(ctx, result)

    # run
    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    fail_fast = bool(getattr(args, "fail_fast", False))
    if use_ui:
        results, returncode = pyfltr.ui.run_commands_with_ui(
            commands,
            args,
            base_ctx,
            archive_hook=composed_hook,
            on_result=_on_result_callback,
            fail_fast=fail_fast,
            only_failed_targets=only_failed_targets,
        )
        # TUI 経路では常に include_details=True（ストリーミングしていないため）。
        ctx = pyfltr.formatters.RunOutputContext(
            config=config,
            output_file=output_file,
            force_text_on_stderr=force_text_on_stderr,
            commands=commands,
            all_files=len(all_files),
            run_id=run_id,
            launcher_prefix=launcher_prefix,
            retry_args_template=retry_args_template,
            stream=False,
            include_details=True,
            structured_stdout=structured_stdout,
        )
    else:
        # 非 TUI モード: 既定はバッファリング (最後にまとめて出力)、`--stream` で従来の即時出力。
        results = pyfltr.cli.run_commands_with_cli(
            commands,
            args,
            base_ctx,
            per_command_log=per_command_log,
            include_fix_stage=include_fix_stage,
            on_result=_on_result_callback,
            archive_hook=composed_hook,
            fail_fast=fail_fast,
            only_failed_targets=only_failed_targets,
        )
        returncode = 0

    # returncode を先に確定させる (render_results に渡して JSONL summary.exit に埋めるため)
    # TUI の Ctrl+C 協調停止は ``run_commands_with_ui`` から 130 (SIGINT 慣例) を返す。
    # この場合は ``calculate_returncode`` で上書きせず、そのまま採用する。
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)

    formatter.on_finish(ctx, results, returncode, pyfltr.warnings_.collected_warnings())

    # アーカイブ終端: meta.json に exit_code / finished_at を書き込む。
    if archive_store is not None and run_id is not None:
        try:
            archive_store.finalize_run(run_id, exit_code=returncode, commands=commands, files=len(all_files))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="archive", message=f"meta.json の更新に失敗: {e}")

    # pre-commit 経由かつ formatter 自動修正発生時の MM 状態ガイダンスを必要に応じて出す。
    _maybe_emit_precommit_guidance(results, structured_stdout=structured_stdout)

    return (returncode, run_id)


_PRECOMMIT_MM_MESSAGE: str = (
    "formatterによる自動修正が発生しました。"
    "`git status`で変更を確認し、必要なら`git add`してから`git commit`を再実行してください。"
)


def _maybe_emit_precommit_guidance(
    results: list[pyfltr.command.CommandResult],
    *,
    structured_stdout: bool,
) -> None:
    """pre-commit 経由かつ formatter 修正発生時に MM 状態ガイダンスを stderr へ出す。

    ``git commit`` から起動された pre-commit 経由で pyfltr が formatter を走らせると、
    修正結果がワークツリーには書き込まれる一方で index には反映されない (MM 状態)。
    この場合に限り ``git add`` を促すメッセージを人間向け (日本語) で出力する。

    構造化 stdout モード (``jsonl`` / ``sarif`` / ``code-quality`` を stdout に流す) では、
    stderr に text が既に流れているため重複を避ける意味でも抑止する。``github-annotations``
    は text と同じレイアウトを stdout に出すため抑止不要。
    """
    if structured_stdout:
        return
    if not any(result.status == "formatted" for result in results):
        return
    if not pyfltr.precommit.is_invoked_from_git_commit():
        return
    print(_PRECOMMIT_MM_MESSAGE, file=sys.stderr)


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
