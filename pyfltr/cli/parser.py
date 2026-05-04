"""argparse構築。

`build_parser()` / `make_common_parent()` とサブコマンド一覧定数を担う。
"""

import argparse
import collections.abc
import pathlib
import shlex
import sys
import typing

import pyfltr.cli.command_info
import pyfltr.cli.mcp_server
import pyfltr.cli.output_format
import pyfltr.cli.shell_completion
import pyfltr.config.config
import pyfltr.output.formatters
import pyfltr.state.runs

_RUN_SUBCOMMANDS: tuple[str, ...] = ("ci", "run", "fast", "run-for-agent")
"""実行系サブコマンド。パイプラインを起動してformat/lint/testを実行する。"""

ALL_SUBCOMMANDS: tuple[str, ...] = (
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

    argparse既定のerror() はエラー文のみを出力してexit 2するため、利用者が正しい書式を
    取り違えたまま同じミスを繰り返しやすい。本サブクラスではエラー文の前に
    `self.print_help(sys.stderr)` を呼び、該当parserのヘルプを併記する。
    サブコマンド側のエラーでは当該サブコマンドのparser、メインの誤サブコマンドでは
    メインのparserのヘルプが出る（argparseの階層別parser_classで継承させるため）。
    """

    def error(self, message: str) -> typing.NoReturn:
        self.print_help(sys.stderr)
        self.exit(2, f"\n{self.prog}: error: {message}\n")


def preflight_tool_name_as_subcommand(sys_args: typing.Sequence[str]) -> None:
    """ツール名をサブコマンドとして入力したケースを検知し、実行例付きメッセージを出力してexit 2。

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
    if candidate in ALL_SUBCOMMANDS:
        return
    tool_names = frozenset(pyfltr.config.config.BUILTIN_COMMAND_NAMES) | frozenset(_STATIC_COMMAND_ALIASES)
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


def make_common_parent(custom_commands: collections.abc.Iterable[str] = ()) -> "_HelpOnErrorArgumentParser":
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
        choices=sorted(pyfltr.output.formatters.FORMATTERS.keys()),
        default=None,
        help=(
            "出力形式を指定します(text/jsonl/sarif/github-annotations/code-quality、既定: text)。"
            "jsonl は LLM 向け JSON Lines 出力、sarif は SARIF 2.1.0、github-annotations は GitHub Actions 向けの注釈形式、"
            "code-quality は GitLab CI の artifacts:reports:codequality 向けの Code Climate JSON issue 形式。"
            f"未指定時は環境変数 {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} の値を使用します。"
            f"さらに環境変数 {pyfltr.cli.output_format.AI_AGENT_ENV} が設定されていれば既定値が jsonl になります"
            f"(優先順位: CLI > {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} > サブコマンド既定値"
            f" > {pyfltr.cli.output_format.AI_AGENT_ENV} > text)。"
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
        "直前 run が存在しない/失敗ツールが無い場合はメッセージを出力して成功終了します。",
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
        "git 不在または ref が存在しない場合は警告を出力して全体実行へフォールバックします。",
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
    for command in pyfltr.config.config.BUILTIN_COMMANDS:
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
    argparseエラー時に該当parserの `--help` 相当をまとめてstderrへ出力する。
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

    common = make_common_parent(custom_commands)

    # サブコマンド別の既定値 （exit_zero_even_if_formatted / commands / output_format ）
    # include_fix_stage) は `apply_subcommand_defaults` で解決する。
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
            f"未指定時は環境変数 {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} を、"
            f"{pyfltr.cli.output_format.AI_AGENT_ENV} が設定されていれば jsonl を採用する"
            f"(優先順位: CLI > {pyfltr.cli.output_format.OUTPUT_FORMAT_ENV} > {pyfltr.cli.output_format.AI_AGENT_ENV} > text)。"
        ),
    )

    # generate-shell-completion: 補完スクリプト出力
    gsc_parser = subparsers.add_parser(
        "generate-shell-completion",
        help="シェル補完スクリプトを出力する。",
    )
    gsc_parser.add_argument(
        "shell",
        choices=pyfltr.cli.shell_completion.SUPPORTED_SHELLS,
        help="出力するシェル種別。",
    )

    # list-runs / show-run: 実行アーカイブの詳細参照サブコマンド
    pyfltr.state.runs.register_subparsers(subparsers)

    # command-info: ツール起動方式（runner / 実行ファイル / 最終コマンドライン等）の解決結果表示
    pyfltr.cli.command_info.register_subparsers(subparsers)

    # mcp: MCPサーバーのstdio起動
    pyfltr.cli.mcp_server.register_subparsers(subparsers)

    return parser


def apply_subcommand_defaults(args: argparse.Namespace) -> None:
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
          サブコマンド既定値`"jsonl"`として注入し、`PYFLTR_OUTPUT_FORMAT`での変更を許容する
    """
    subcommand = args.subcommand
    args.include_fix_stage = subcommand in ("run", "fast", "run-for-agent")
    if subcommand in ("run", "fast", "run-for-agent"):
        args.exit_zero_even_if_formatted = True
    if subcommand == "fast" and args.commands is None:
        # `--commands` は `action="append"` 化によりリストで保持する。
        args.commands = ["fast"]
