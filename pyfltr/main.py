#!/usr/bin/env python3
"""pyfltr。"""

# v3.0.0でサブコマンド・実行アーカイブ・キャッシュ・retry_command絞り込みなどを
# 段階的に集約した。retry_command生成はpyfltr.retryモジュールが担う。

import argparse
import collections.abc
import contextlib
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
import pyfltr.command_info
import pyfltr.config
import pyfltr.config_cli
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
# 実行系 （ci / run / fast / run-for-agent） は共通オプション （_COMMON_PARENT） を継承する。
# それ以外のサブコマンド （config / generate-shell-completion） は固有の引数のみ持つ。
_RUN_SUBCOMMANDS: tuple[str, ...] = ("ci", "run", "fast", "run-for-agent")
"""実行系サブコマンド。パイプラインを起動してformat/lint/testを走らせる。"""

_ALL_SUBCOMMANDS: tuple[str, ...] = (
    *_RUN_SUBCOMMANDS,
    "config",
    "generate-shell-completion",
    "list-runs",
    "show-run",
    "command-info",
    "mcp",
)
"""全サブコマンド。shell completionスクリプト生成時に参照される。"""


_STATIC_COMMAND_ALIASES: tuple[str, ...] = ("format", "lint", "test")
"""組み込みで必ず定義されるコマンドエイリアス。ユーザー設定のカスタムエイリアスは含まない。

ツール名プリフライト（個別ツール絞り込み誘導）の検出集合に含める。
カスタムコマンド・カスタムエイリアスはpyproject.toml読込後にしか確定しないため
当面プリフライト対象外とする。
"""


class _HelpOnErrorArgumentParser(argparse.ArgumentParser):
    """argparseエラー時に `--help` 相当をstderrに併記してから終了するArgumentParser。

    argparse既定のerror() はエラー文のみを出してexit 2するため、利用者が正しい書式を
    取り違えたまま同じミスを繰り返しやすい。本サブクラスではエラー文の前に
    `self.print_help(sys.stderr)` を呼び、該当parserのヘルプを併記する。
    サブコマンド側のエラーでは当該サブコマンドのparser、メインの誤サブコマンドでは
    メインのparserのヘルプが出る（argparseの階層別parser_classで継承させるため）。
    """

    def error(self, message: str) -> typing.NoReturn:
        self.print_help(sys.stderr)
        self.exit(2, f"\n{self.prog}: error: {message}\n")


def _preflight_tool_name_as_subcommand(sys_args: typing.Sequence[str]) -> None:
    """ツール名をサブコマンドとして入力したケースを検知し、実行例付きメッセージを出してexit 2。

    `uv run pyfltr textlint ...` / `pyfltr lint docs/` など、利用者がツール名またはエイリアスを
    そのままサブコマンドとして指定した場合に、正しい `--commands=<tool>` 書式の実行例を提示する。
    該当しない場合は何も行わずに返し、通常のargparse処理を続行する。

    検出対象は `pyfltr.builtin_commands.BUILTIN_COMMAND_NAMES` + 静的エイリアス
    `format` / `lint` / `test`。カスタムコマンドはpyproject.toml読込後にしか
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
    pyfltr.command.dedupe_environ_path(os.environ)
    exit_code = run()
    logger.debug(f"{exit_code=}")
    sys.exit(exit_code)


def _make_common_parent(custom_commands: collections.abc.Iterable[str] = ()) -> "_HelpOnErrorArgumentParser":
    """実行系サブコマンド用の共通オプションをまとめた親parserを返す。

    `parents=[common]` 経由で各サブコマンドに継承させる。`custom_commands` は
    `pyproject.toml` で定義されたカスタムコマンド名の列で、`--{cmd}-args`
    オプションとして追加登録される （ビルトインと同じ扱い）。
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
        action="append",
        help="対象のコマンド一覧を指定します。複数回指定可能で、各値はカンマ区切りも併用可能です。"
        "例: --commands=mypy --commands=pyright,ruff-check は"
        " --commands=mypy,pyright,ruff-check と同等です。"
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
        help=(
            "出力形式を指定します(text/jsonl/sarif/github-annotations/code-quality、既定: text)。"
            "jsonl は LLM 向け JSON Lines 出力、sarif は SARIF 2.1.0、github-annotations は GitHub Actions 向けの注釈形式、"
            "code-quality は GitLab CI の artifacts:reports:codequality 向けの Code Climate JSON issue 形式。"
            f"未指定時は環境変数 {pyfltr.cli.OUTPUT_FORMAT_ENV} の値を使用します。"
            f"さらに環境変数 {pyfltr.cli.AI_AGENT_ENV} が設定されていれば既定値が jsonl になります"
            f"(優先順位: CLI > {pyfltr.cli.OUTPUT_FORMAT_ENV} > サブコマンド既定値 > {pyfltr.cli.AI_AGENT_ENV} > text)。"
        ),
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
        help="ツールの構造化出力(JSON等)を無効化し、人間向けの元のテキスト出力を使用します。",
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
        help="--only-failed の参照対象 run を明示指定します(前方一致 / latest 対応)。未指定時は直前 run を自動選択します。",
    )
    common.add_argument(
        "--changed-since",
        default=None,
        metavar="REF",
        help="git の任意の ref(ブランチ・タグ・コミットハッシュ・HEAD など)を指定し、"
        "その ref からの変更ファイルのみを対象とします。"
        "コミット差分・未コミット作業ツリー差分・staged 差分の和集合で絞り込みます。"
        "git 不在または ref が存在しない場合は警告を出して全体実行へフォールバックします。",
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

    # 各コマンド用の引数追加オプション （ビルトイン + カスタム）
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
    """引数パーサーを生成。サブコマンド必須化 （v3.0.0）。

    メインおよび全サブコマンドparserには `_HelpOnErrorArgumentParser` を用い、
    argparseエラー時に該当parserの `--help` 相当をまとめてstderrへ出す。
    """
    parser = _HelpOnErrorArgumentParser(
        epilog=(
            "サブコマンド:\n"
            "  ci               CI モードで実行する。フォーマッターの変更も失敗扱い。\n"
            "  run              通常実行。フォーマッターの変更は成功扱いで fix ステージ有効。\n"
            "  fast             高速ツールのみ実行 (--commands=fast 相当)。\n"
            "  run-for-agent    LLM エージェント向け (JSONL 出力を既定化)。\n"
            "  config <action>  設定ファイルを操作する (get / set / delete / list)。\n"
            "  generate-shell-completion <shell>\n"
            "                   シェル補完スクリプトを出力する (bash / powershell)。\n"
            "  list-runs        実行アーカイブ内の run 一覧を表示する。\n"
            "  show-run <run_id>\n"
            "                   指定 run の詳細 (meta・ツール別サマリ・diagnostic・生出力) を表示する。\n"
            "  command-info <command>\n"
            "                   ツール起動方式(runner / 実行ファイル / 最終コマンドライン等)の解決結果を表示する。\n"
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

    # サブコマンド別の既定値 （exit_zero_even_if_formatted / commands / output_format ）
    # include_fix_stage) は `_apply_subcommand_defaults` で解決する。
    # subparser単位の `set_defaults` は `parents=[common]` で共有された
    # 名前空間を通じて他サブパーサーのdefaultも書き換えてしまうため採用しない
    # （argparseの既知挙動）。
    subparsers.add_parser("ci", parents=[common], help="CI モードで実行する。")
    subparsers.add_parser("run", parents=[common], help="通常実行。")
    subparsers.add_parser("fast", parents=[common], help="高速ツールのみ実行。")
    subparsers.add_parser("run-for-agent", parents=[common], help="LLM エージェント向け。")

    # config: 設定ファイル操作（pnpm/npm config互換のget/set/delete/list）
    config_parser = subparsers.add_parser("config", help="設定ファイルを操作する。")
    config_subparsers = config_parser.add_subparsers(
        dest="config_action",
        required=True,
        metavar="<action>",
        parser_class=_HelpOnErrorArgumentParser,
    )

    config_get = config_subparsers.add_parser("get", help="設定値を取得する。")
    config_get.add_argument("key", help="設定キー名 (例: archive-max-age-days)。")
    config_get.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="グローバル設定ファイルを対象にする。",
    )

    config_set = config_subparsers.add_parser("set", help="設定値を書き込む。")
    config_set.add_argument("key", help="設定キー名 (例: archive-max-age-days)。")
    config_set.add_argument("value", help="設定値 (型に応じて変換される)。")
    config_set.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="グローバル設定ファイルを対象にする (不在時は自動作成)。",
    )

    config_delete = config_subparsers.add_parser("delete", help="設定値を削除する。")
    config_delete.add_argument("key", help="設定キー名。")
    config_delete.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="グローバル設定ファイルを対象にする。",
    )

    config_list = config_subparsers.add_parser("list", help="現在の設定値を一覧表示する。")
    config_list.add_argument(
        "--global",
        dest="global_",
        action="store_true",
        help="グローバル設定ファイルを対象にする。",
    )
    config_list.add_argument(
        "--output-format",
        choices=["text", "json", "jsonl"],
        default=None,
        help=(
            "出力形式 (text / json / jsonl、既定: text)。"
            f"未指定時は環境変数 {pyfltr.cli.OUTPUT_FORMAT_ENV} を、"
            f"{pyfltr.cli.AI_AGENT_ENV} が設定されていれば jsonl を採用する"
            f"(優先順位: CLI > {pyfltr.cli.OUTPUT_FORMAT_ENV} > {pyfltr.cli.AI_AGENT_ENV} > text)。"
        ),
    )

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

    # command-info: ツール起動方式（runner / 実行ファイル / 最終コマンドライン等）の解決結果表示
    pyfltr.command_info.register_subparsers(subparsers)

    # mcp: MCPサーバーのstdio起動
    pyfltr.mcp_.register_subparsers(subparsers)

    return parser


def _apply_subcommand_defaults(args: argparse.Namespace) -> None:
    """サブコマンドごとの既定値を `args` に反映する。

    `subparsers.add_parser(..., parents=[common])` で共通オプションを継承する
    構造上、`sub_parser.set_defaults(...)` は他サブパーサーのdefaultまで
    上書きしてしまうため （argparseの既知挙動）、argparse本体の既定値機構は
    使わずここで手動解決する。CLI明示値 （`store_true` や値指定） は
    事前にargsに載っているため、既定値注入は「未指定扱いの値」を上書きする
    形にとどめる。

    サブコマンド挙動:
        - `ci`: fixステージ無効。exit_zero_even_if_formattedは明示時のみTrue
        - `run`: fixステージ有効。exit_zero_even_if_formattedをTrueに
        - `fast`: runと同じ + `--commands` 未指定なら `"fast"`
        - `run-for-agent`: runと同じ。`--output-format`の既定値は`_resolve_output_format`側で
          サブコマンド既定値`"jsonl"`として注入し、`PYFLTR_OUTPUT_FORMAT`での切り戻しを許す
    """
    subcommand = args.subcommand
    args.include_fix_stage = subcommand in ("run", "fast", "run-for-agent")
    if subcommand in ("run", "fast", "run-for-agent"):
        args.exit_zero_even_if_formatted = True
    if subcommand == "fast" and args.commands is None:
        # `--commands` は `action="append"` 化によりリストで保持する。
        args.commands = ["fast"]


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
    _preflight_tool_name_as_subcommand(sys_args)

    parser = build_parser()
    args = parser.parse_args(list(sys_args))
    subcommand = args.subcommand
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO, format="%(message)s")

    # configサブコマンド: 設定ファイルの取得・編集（pnpm/npm config互換）
    if subcommand == "config":
        return pyfltr.config_cli.execute(parser, args)

    # generate-shell-completionサブコマンド: 補完スクリプトをstdoutに出力する
    if subcommand == "generate-shell-completion":
        # 補完スクリプト側は「サブコマンド + 共通オプション一式」を列挙する必要があるため、
        # 実行系サブコマンドの共通parentを渡す （カスタムコマンドは対象外で十分）。
        script = pyfltr.shell_completion.generate(args.shell, _make_common_parent(), frozenset(_ALL_SUBCOMMANDS))
        print(script, end="")
        return 0

    # 実行アーカイブの詳細参照サブコマンド: load_config() を呼ばずarchiveのみを参照する。
    if subcommand == "list-runs":
        return pyfltr.runs.execute_list_runs(parser, args)
    if subcommand == "show-run":
        return pyfltr.runs.execute_show_run(parser, args)

    # command-info: 対象ツールの起動方式・解決結果を表示する（実行はしない）
    if subcommand == "command-info":
        return pyfltr.command_info.execute_command_info(args)

    # MCPサーバーサブコマンド: stdioでFastMCPサーバーを起動する。
    if subcommand == "mcp":
        return pyfltr.mcp_.execute_mcp(args)

    # サブコマンド別の既定値を注入する （CLI明示値が優先）。
    _apply_subcommand_defaults(args)

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
    try:
        return _run_impl(parser, args, list(sys_args), resolved_targets, original_cwd=original_cwd)
    finally:
        if chdir_applied:
            os.chdir(original_cwd)


def _flatten_commands_arg(values: list[str] | None, config: pyfltr.config.Config) -> list[str]:
    """`--commands` で渡されたリスト（複数回指定の集合）をコマンド名配列に展開する。

    各要素にはカンマ区切りで複数のコマンドを含められるため、splitした上で
    先頭出現を優先した重複除去を行う。`None` の場合は設定上の全登録コマンド
    （ビルトイン + custom-commands）を返す。
    """
    if values is None:
        return list(config.command_names)
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        for name in raw.split(","):
            if name == "" or name in seen:
                continue
            seen.add(name)
            result.append(name)
    return result


_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(pyfltr.formatters.FORMATTERS.keys())


def _resolve_output_format(parser: argparse.ArgumentParser, args: argparse.Namespace) -> str:
    """実行系サブコマンド向けに出力形式を解決する。

    `pyfltr.cli.resolve_output_format`へ委譲し、`run-for-agent`のみサブコマンド既定値`"jsonl"`を
    渡す。これにより`PYFLTR_OUTPUT_FORMAT=text`で`run-for-agent`の既定をtextへ切り戻せる。
    """
    subcommand_default = "jsonl" if args.subcommand == "run-for-agent" else None
    return pyfltr.cli.resolve_output_format(
        parser,
        args.output_format,
        valid_values=_VALID_OUTPUT_FORMATS,
        subcommand_default=subcommand_default,
    )


def _run_impl(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    original_sys_args: typing.Sequence[str],
    resolved_targets: list[pathlib.Path] | None,
    *,
    original_cwd: str,
) -> int:
    """run()の内部実装（実行系サブコマンド向け）。"""
    # 同一プロセス内でrun() が複数回呼ばれるケースに備えて警告蓄積を初期化する。
    pyfltr.warnings_.clear()

    # --ciオプションの処理
    if args.ci:
        args.shuffle = False
        args.no_ui = True

    # --from-runは--only-failedとの併用が必須。
    # 単独利用を許可しない理由: --from-run単独ではdiagnostic参照は行われず、
    # 「再実行対象を指定runの失敗ツールに絞り込む」という本来の意味を持たない。
    # argparse段階で拒否することでユーザーに正しい併用形を即座に提示できる。
    if getattr(args, "from_run", None) is not None and not getattr(args, "only_failed", False):
        parser.error("argument --from-run: requires --only-failed")

    # --uiと--no-uiの競合チェック
    if args.ui and args.no_ui:
        parser.error("--ui と --no-ui は同時に指定できません。")

    # --version （実行系サブコマンド下でも許容）
    if args.version:
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    output_format = _resolve_output_format(parser, args)
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

    # --commands未指定時はカスタムコマンドを含む全登録コマンドを対象にする。
    # argparseのデフォルト評価時点ではpyproject.tomlを読み込んでいないため、
    # ビルトインのみのdefaultを返すとcustom-commandsが常にスキップされる。
    # load_config後に実体を決定することで、ユーザーが登録したcustom-commands
    # （例: svelte-check） も `run` / `ci` サブコマンドのデフォルト動作で走るようにする。
    # `--commands` は `action="append"` によりリストで渡るため、各要素を
    # カンマ区切りで再分割して平坦化する。重複は先出を優先して除去する。
    commands: list[str] = pyfltr.config.resolve_aliases(_flatten_commands_arg(args.commands, config), config)
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

    `force_text_on_stderr=True` を渡すと、人間向けtext整形ログの出力先を
    stdoutではなくstderrに強制する（MCP経路でstdoutをJSON-RPCフレームが
    占有するケース用）。

    Returns:
        `(exit_code, run_id)` のタプル。
        `exit_code` は0 = 成功、1 = 失敗。
        `run_id` は実行アーカイブが有効で採番に成功した場合のULID文字列、
        無効・採番失敗・early exit時は `None`。
        `--only-failed` 指定で「直前runなし」「失敗ツールなし」「対象ファイル
        交差が空」のいずれかに該当する場合はearly exitとして `(0, None)` を
        返す。MCP経路はこの `run_id is None` を「実行スキップ」として識別する。

    タプル戻り値を採用したのはMCP経路がrun_idを確実に取得するため。
    代替案としてMCP側で `ArchiveStore.list_runs(limit=1)` を引く案も検討
    したが、同一ユーザーキャッシュを参照する並行プロセスがあると別runの
    `run_id` を誤って拾うリスクがあるため戻り値経由とした。
    """
    output_format = args.output_format or "text"
    output_file: pathlib.Path | None = args.output_file
    # JSONL / SARIF / code-qualityのstdoutモードではstdoutを構造化出力が占有するため、
    # UI・画面クリア・streamによる詳細ログ即時出力を無効化する。
    structured_stdout = output_format in ("jsonl", "sarif", "code-quality") and output_file is None
    if structured_stdout:
        args.ui = None
        args.no_ui = True
        args.no_clear = True
        args.stream = False

    formatter = pyfltr.formatters.FORMATTERS[output_format]()

    # loggerを初期化する。同一プロセスでrun_pipelineが複数回呼ばれるMCP経路でも、
    # format / output_file / force_text_on_stderrの組み合わせで出力先が切り替わるため、毎回張り直す。
    # configure_loggersはoutput_file / force_text_on_stderrのみ参照するため、
    # run_id等が未確定の段階でも呼び出せる（残フィールドはデフォルト値のまま渡す）。
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

    # --changed-since指定時はgit差分ファイルとの交差に絞り込む。
    # --only-failedよりも先に適用し、以後のフィルタは絞り込み済みリストを受け取る。
    changed_since_ref: str | None = getattr(args, "changed_since", None)
    if changed_since_ref is not None:
        all_files = pyfltr.command.filter_by_changed_since(all_files, changed_since_ref)

    # --only-failed指定時は直前runからツール別の失敗ファイル集合を構築する。
    # archive / cache初期化より前に実行し、早期終了の場合はそれらの副作用を発生させない。
    commands, only_failed_targets, only_failed_exit_early = pyfltr.only_failed.apply_filter(
        args, commands, all_files, from_run=getattr(args, "from_run", None)
    )
    if only_failed_exit_early:
        return 0, None

    # 実行対象として有効化されていないコマンドはパイプラインから除外する。
    # split_commands_for_executionと同じ条件 （`config.values.get(cmd) is True`） で絞り込み、
    # JSONL header・実行アーカイブ・formatter ctxへ渡すcommandsを「実際に実行されるもの」に統一する。
    commands = [c for c in commands if config.values.get(c) is True]

    # retry_command再構成用のベース情報を確定する。original_cwdはrun() が保存した
    # --work-dir適用前のcwd、original_sys_argsは起動時のsys.argv[1:] のコピー。
    effective_cwd = original_cwd if original_cwd is not None else os.getcwd()
    effective_sys_args = list(original_sys_args) if original_sys_args is not None else list(sys.argv[1:])
    launcher_prefix = pyfltr.retry.detect_launcher_prefix()
    retry_args_template = pyfltr.retry.build_retry_args_template(effective_sys_args)

    # 実行アーカイブの初期化 （既定で有効）。
    # `--no-archive` または `archive = false` で無効化できる。クリーンアップ失敗や
    # 書き込み失敗はパイプライン本体を止めないようwarningsへ流す。
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

    # 実行環境の情報を出力（run_id採番後にまとめて出すことで区切り線内に含める）。
    pyfltr.cli.text_logger.info(f"{'-' * 10} pyfltr {'-' * (72 - 10 - 8)}")
    pyfltr.cli.text_logger.info(f"version:        {importlib.metadata.version('pyfltr')}")
    pyfltr.cli.text_logger.info(f"sys.executable: {sys.executable}")
    pyfltr.cli.text_logger.info(f"sys.version:    {sys.version}")
    pyfltr.cli.text_logger.info(f"cwd:            {os.getcwd()}")
    if run_id is not None:
        launcher_cmd = shlex.join(launcher_prefix)
        pyfltr.cli.text_logger.info("run_id:         %s(`%s show-run %s` で詳細を確認可能)", run_id, launcher_cmd, run_id)
    pyfltr.cli.text_logger.info("-" * 72)

    # ファイルhashキャッシュの初期化 （既定で有効）。
    # `--no-cache` または `cache = false` で無効化できる。期間超過エントリの削除失敗や
    # 書き込み失敗はパイプライン本体を止めないためwarningsに流す。
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
                # ハンドラ内でwarningを出してもsummary末尾にまとまる。
                pyfltr.warnings_.emit_warning(source="archive", message=f"{result.command} のアーカイブ書き込みに失敗: {e}")
                return
            # 書き込み成功時のみarchived=Trueに更新。smart truncationの可否判定に使う。
            result.archived = True

        archive_hook = _archive_hook

    # retry_commandをCommandResultに埋めるためのヘルパー。
    # archive_hookと同じタイミング （各ツール完了時） に呼ばれるon_result経路へ挿入する。
    # 実装本体は `_populate_retry_command` （A案の失敗ファイル絞り込み・cached
    # 判定を含む） に委譲し、クロージャ変数をキーワード引数で引き渡す。
    def _attach_retry_command(result: pyfltr.command.CommandResult) -> None:
        pyfltr.retry.populate_retry_command(
            result,
            retry_args_template=retry_args_template,
            launcher_prefix=launcher_prefix,
            original_cwd=effective_cwd,
        )

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.ui.can_use_ui())

    # run_pipelineが1回だけ組み立てる不変コンテキスト。
    # archive_storeはhook経由で渡すためContextには含めない。
    base_ctx = pyfltr.command.ExecutionBaseContext(
        config=config,
        all_files=all_files,
        cache_store=cache_store,
        cache_run_id=run_id,
    )

    # 各ツール完了時のフック: retry_command付与 → archive書き込み → formatter.on_result （ストリーミング等）。
    # retry_commandはarchiveとJSONL streamingの双方で必要になるため、archive_hookより前に挿入する。
    # formatter.on_resultはarchive_hookの後に呼ぶ（result.archived=Trueが立った後）。
    # on_start / on_result / on_finishで使う完全なctxを構築する。
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
    #   1. _attach_retry_command(result) → retry_commandをresultに付与
    #   2. archive_hook(result) → アーカイブ書き込み（cachedの場合はスキップ）
    #   3. formatter.on_result(ctx, result) → JSONL streamingなど（cachedでも呼ばれる）
    # 上記1+2をcomposed_hookにまとめ、3はrun_commands_with_cliのon_result引数として渡す。
    # これによりcachedの場合でもformatter.on_resultが呼ばれる（cli.pyの設計を踏襲）。
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
        # TUI経路では常にinclude_details=True（ストリーミングしていないため）。
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
        # 非TUIモード: 既定はバッファリング （最後にまとめて出力）、`--stream` で従来の即時出力。
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

    # returncodeを先に確定させる （render_resultsに渡してJSONL summary.exitに埋めるため）
    # TUIのCtrl+C協調停止は `run_commands_with_ui` から130 （SIGINT慣例） を返す。
    # この場合は `calculate_returncode` で上書きせず、そのまま採用する。
    if returncode == 0:
        returncode = calculate_returncode(results, args.exit_zero_even_if_formatted)

    formatter.on_finish(ctx, results, returncode, pyfltr.warnings_.collected_warnings())

    # アーカイブ終端: meta.jsonにexit_code / finished_atを書き込む。
    if archive_store is not None and run_id is not None:
        try:
            archive_store.finalize_run(run_id, exit_code=returncode, commands=commands, files=len(all_files))
        except OSError as e:
            pyfltr.warnings_.emit_warning(source="archive", message=f"meta.json の更新に失敗: {e}")

    # pre-commit経由かつformatter自動修正発生時のMM状態ガイダンスを必要に応じて出す。
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
    """pre-commit経由かつformatter修正発生時にMM状態ガイダンスをstderrへ出す。

    `git commit` から起動されたpre-commit経由でpyfltrがformatterを走らせると、
    修正結果がワークツリーには書き込まれる一方でindexには反映されない （MM状態）。
    この場合に限り `git add` を促すメッセージを人間向け （日本語） で出力する。

    構造化stdoutモード （`jsonl` / `sarif` / `code-quality` をstdoutに流す） では、
    stderrにtextが既に流れているため重複を避ける意味でも抑止する。`github-annotations`
    はtextと同じレイアウトをstdoutに出すため抑止不要。
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
    if any(status in {"failed", "resolution_failed"} for status in statuses):
        return 1
    if not exit_zero_even_if_formatted and any(status == "formatted" for status in statuses):
        return 1
    return 0


if __name__ == "__main__":
    main()
