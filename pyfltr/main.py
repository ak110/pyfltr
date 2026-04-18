#!/usr/bin/env python3
"""pyfltr。"""

# v3.0.0 でサブコマンド・実行アーカイブ・キャッシュ・retry_command 絞り込みなどを
# 段階的に集約したため本モジュールは 1000 行上限を超える。将来の整理候補として
# retry_command / --only-failed 系ヘルパー (_filter_failed_files・_populate_retry_command・
# _apply_only_failed_filter 等) の別モジュール分割がある。
# pylint: disable=too-many-lines

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
import pyfltr.cache
import pyfltr.cli
import pyfltr.command
import pyfltr.config
import pyfltr.github_annotations
import pyfltr.llm_output
import pyfltr.mcp_
import pyfltr.runs
import pyfltr.sarif_output
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
        choices=("text", "jsonl", "sarif", "github-annotations"),
        default=None,
        help="出力形式を指定します(text/jsonl/sarif/github-annotations、既定: text)。"
        "jsonl は LLM 向け JSON Lines 出力、sarif は SARIF 2.1.0、github-annotations は GitHub Actions 向けの注釈形式。"
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
_VALID_OUTPUT_FORMATS: frozenset[str] = frozenset({"text", "jsonl", "sarif", "github-annotations"})
_STRUCTURED_OUTPUT_FORMATS: frozenset[str] = frozenset({"jsonl", "sarif", "github-annotations"})


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


def _force_structured_stdout_mode(args: argparse.Namespace) -> None:
    """構造化出力 + stdout モード時、UI/進捗系オプションを silently 無効化する。

    `parser.error()` で拒否すると argparse が usage を stderr に書くため
    「stdout/stderr とも完全に抑止」の要件に反する。既存 `--ci` が
    `args.no_ui=True` を silently 強制する先例に揃えて無音で上書きする。
    jsonl だけでなく sarif / github-annotations でも stdout を専有するため
    共通ヘルパーとして使う。
    """
    args.ui = None
    args.no_ui = True
    args.no_clear = True
    args.stream = False


def _suppress_logging() -> tuple[list[logging.Handler], int]:
    """Root logger の handlers と level を保存しつつ完全抑止する。

    復元値をタプルで返す。復元は `_restore_logging()` で行う。`run()` は
    同一プロセスで複数回呼ばれる設計のため、必ず呼び出し側で `try`/`finally`
    による復元を保証すること。jsonl / sarif / github-annotations のいずれも
    stdout を構造化出力が占有するため、共通で root logger を抑止する。
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


def _detect_launcher_prefix() -> list[str]:
    """retry_command の先頭に置くべき起動プレフィックスを推定する。

    Linux では ``/proc/self/status`` から親プロセスを辿り、先頭が ``uv`` で
    第 2 引数が ``run`` なら ``["uv", "run", "pyfltr"]``、先頭が ``uvx`` なら
    ``["uvx", "pyfltr"]`` を返す。macOS / Windows など親プロセスを取得できない
    環境では ``[sys.argv[0]]`` の basename にフォールバックする。
    """
    fallback = [os.path.basename(sys.argv[0]) or "pyfltr"]
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            ppid_line = next((line for line in f if line.startswith("PPid:")), None)
        if ppid_line is None:
            return fallback
        ppid = ppid_line.split(":", 1)[1].strip()
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            raw = f.read()
    except OSError:
        return fallback
    tokens = [tok.decode("utf-8", errors="replace") for tok in raw.split(b"\0") if tok]
    if not tokens:
        return fallback
    launcher = os.path.basename(tokens[0])
    if launcher == "uv" and len(tokens) >= 2 and tokens[1] == "run":
        return ["uv", "run", "pyfltr"]
    if launcher == "uvx":
        return ["uvx", "pyfltr"]
    return fallback


def _build_retry_args_template(sys_args: list[str]) -> list[str]:
    """起動時 argv (サブコマンド以降) を retry_command 用テンプレートとして整形する。

    ``--commands`` の値は後段の per-tool 差し替え用に空文字プレースホルダへ置換し、
    位置引数 (ターゲット) は末尾から除去する (後段で当該ツールのファイル一覧で
    置換される前提)。``--no-fix`` や ``--output-format`` 等は保持する。
    """
    result: list[str] = []
    i = 0
    # 実行系サブコマンド (ci / run / fast / run-for-agent) は先頭に必ず来る想定。
    # これを保持することで pyfltr ci 失敗時に fix ステージが暴発しない。
    while i < len(sys_args):
        arg = sys_args[i]
        if arg == "--commands":
            # 後段で当該ツール 1 件に差し替えるためプレースホルダを置く。
            result.extend([arg, ""])
            i += 2
            continue
        if arg.startswith("--commands="):
            result.append("--commands=")
            i += 1
            continue
        result.append(arg)
        i += 1
    return result


def _apply_only_failed_filter(
    args: argparse.Namespace,
    commands: list[str],
    all_files: list[pathlib.Path],
) -> tuple[list[str], dict[str, list[pathlib.Path] | None] | None, bool]:
    """``--only-failed`` 指定時、直前 run からツール別の失敗ファイル集合を構築する。

    Returns:
        ``(絞り込み後 commands, per_tool_targets, exit_early)``
        - ``args.only_failed`` が偽の場合は ``(commands, None, False)`` を返す (未適用)
        - 直前 run が存在しない / アーカイブ読み取り失敗 / 失敗ツールが無い / 全ツール
          で ``targets`` 交差が空の場合は ``(commands, None, True)`` を返し、呼び出し側で
          ``rc=0`` の早期終了を促す
        - それ以外は失敗ツール集合と、ツール別の失敗ファイル集合 dict を返す。
          値 ``None`` は「直前 run に診断ファイルが無く全体再実行が必要 (pytest 等)」、
          空リストは「交差空で除外」を意味する。戻りの ``commands`` からは除外済み。

    ``all_files`` は ``expand_all_files`` の結果 (``args.targets`` 指定があれば既に
    その範囲に絞り込まれている)。本関数ではそれとの交差を取ることで
    ``--only-failed`` と位置引数 ``targets`` の併用要件を同時に満たす。
    """
    if not getattr(args, "only_failed", False):
        return commands, None, False

    try:
        store = pyfltr.archive.ArchiveStore()
        runs = store.list_runs(limit=1)
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"実行アーカイブを読み取れません: {e}",
        )
        return commands, None, True

    if not runs:
        logger.info("--only-failed: 参照可能な直前 run が見つかりません。対象なしでスキップします。")
        return commands, None, True

    last_run = runs[0]
    try:
        tool_names = store.list_tools(last_run.run_id)
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"直前 run のツール一覧を読み取れません: {e}",
        )
        return commands, None, True

    # all_files を正規化済み相対パス文字列でキーに持つ辞書にしておき、
    # 診断側の ``file`` (``_normalize_path`` 済み) とそのまま比較する。
    all_files_map: dict[str, pathlib.Path] = {}
    for path in all_files:
        all_files_map[str(path).replace("\\", "/")] = path

    commands_set = set(commands)
    per_tool_targets: dict[str, list[pathlib.Path] | None] = {}
    failed_tools: list[str] = []
    for tool in tool_names:
        if tool not in commands_set:
            continue
        try:
            meta = store.read_tool_meta(last_run.run_id, tool)
        except OSError:
            continue
        if meta.get("status") != "failed":
            continue
        failed_tools.append(tool)
        try:
            diagnostics = store.read_tool_diagnostics(last_run.run_id, tool)
        except OSError:
            diagnostics = []
        failed_files = {d["file"] for d in diagnostics if isinstance(d.get("file"), str)}
        if not failed_files:
            # 診断ファイル無し (pass-filenames=False のツール等) → 既定対象でフォールバック実行
            per_tool_targets[tool] = None
            continue
        # ``all_files_map`` を基準に走査することで ``_filter_failed_files`` と同じく
        # target 側の並び順を保つ (``failed_files`` を直接反復するとセット由来の順序不定が混入する)。
        intersected = [path for key, path in all_files_map.items() if key in failed_files]
        per_tool_targets[tool] = intersected

    if not failed_tools:
        logger.info(f"--only-failed: 直前 run ({last_run.run_id}) に失敗ツールがありません。対象なしでスキップします。")
        return commands, None, True

    # per_tool_targets 値が [] のツールは「交差空 → 除外」。commands 順を保って絞り込む。
    filtered_commands = [cmd for cmd in commands if cmd in failed_tools and per_tool_targets.get(cmd) != []]
    if not filtered_commands:
        logger.info(
            f"--only-failed: 直前 run ({last_run.run_id}) の失敗ツールはすべて指定 targets と交差しません。"
            "対象なしでスキップします。"
        )
        return commands, None, True

    filtered_per_tool_targets = {cmd: per_tool_targets[cmd] for cmd in filtered_commands}
    logger.info(f"--only-failed: 直前 run ({last_run.run_id}) から {len(filtered_commands)} ツールを対象として再実行します。")
    return filtered_commands, filtered_per_tool_targets, False


def _filter_failed_files(result: pyfltr.command.CommandResult) -> list[pathlib.Path]:
    """``result.errors`` から失敗ファイル集合を抽出し ``result.target_files`` と交差させる。

    ``retry_command`` のターゲットを「当該ツールで失敗したファイルのみ」に絞る用途
    (パートG A案)。パス比較は文字列化した相対パス (スラッシュ区切り) で行う。
    ``ErrorLocation.file`` は ``_normalize_path`` 経由で cwd 基準の相対パス (区切り
    文字は ``/``) に正規化されているため、``result.target_files`` 側も同じ表現へ
    揃えたうえで比較する。並び順は ``result.target_files`` の順序を保つ。

    ``result.errors`` が空、または ``ErrorLocation.file`` 集合と ``result.target_files``
    の交差が空の場合は空リストを返す (pytest 等の pass-filenames=False で全体失敗
    のみのケース。呼び出し側で retry_command のターゲット位置引数を空にする前提)。
    """
    if not result.errors:
        return []
    failed_files = {error.file for error in result.errors if error.file}
    if not failed_files:
        return []
    filtered: list[pathlib.Path] = []
    for target in result.target_files:
        normalized = str(target).replace("\\", "/")
        if normalized in failed_files:
            filtered.append(target)
    return filtered


def _populate_retry_command(
    result: pyfltr.command.CommandResult,
    *,
    retry_args_template: list[str],
    launcher_prefix: list[str],
    original_cwd: str,
) -> None:
    """CommandResult に retry_command を埋める (パートG A案の絞り込みを適用)。

    キャッシュ復元結果 (``result.cached == True``) では retry_command を埋めない
    (再実行不要のため)。それ以外では ``_filter_failed_files`` で失敗ファイルのみに
    絞り込んだターゲットを ``_build_retry_command`` へ渡す。絞り込み結果が空の場合
    (診断ファイルなし・全体失敗のみのケース) は retry_command のターゲット位置
    引数が空になる (当該ツールの単体再実行文字列として機能する)。
    """
    if result.cached:
        return
    filtered_targets = _filter_failed_files(result)
    result.retry_command = _build_retry_command(
        retry_args_template,
        launcher_prefix,
        tool=result.command,
        target_files=filtered_targets,
        original_cwd=original_cwd,
    )


def _build_retry_command(
    args_template: list[str],
    launcher_prefix: list[str],
    *,
    tool: str,
    target_files: list[pathlib.Path],
    original_cwd: str,
) -> str:
    """Tool レコードへ埋め込む retry_command 文字列を生成する。

    ``args_template`` の ``--commands`` プレースホルダを当該ツールに差し替え、
    位置引数 (ターゲット) を ``target_files`` で末尾に再配置する。ターゲットは
    ``original_cwd`` 基準の絶対パスに変換することで、``--work-dir`` と cwd の
    二重解釈を避ける。
    """
    import shlex  # pylint: disable=import-outside-toplevel

    cwd_path = pathlib.Path(original_cwd)
    # 位置引数 (サブコマンドを除く、`-` で始まらないトークンの末尾側) は除去する。
    # サブコマンドは必ず最初の非オプショントークンのため、それだけ残す。
    filtered: list[str] = []
    seen_subcommand = False
    i = 0
    while i < len(args_template):
        arg = args_template[i]
        # オプション引数 (=付き・=なし両対応) はそのまま保持する。
        # ``--commands`` プレースホルダ経由のみ後段で差し替え対象とする。
        if arg.startswith("-"):
            filtered.append(arg)
            # 引数を伴うオプションか確認 (値が付いていない場合は次のトークンも引き取る)。
            if "=" not in arg and i + 1 < len(args_template):
                next_arg = args_template[i + 1]
                if not next_arg.startswith("-") and _option_takes_value(arg):
                    filtered.append(next_arg)
                    i += 2
                    continue
            i += 1
            continue
        # 最初の非オプショントークンはサブコマンド扱いで保持する。
        if not seen_subcommand:
            filtered.append(arg)
            seen_subcommand = True
            i += 1
            continue
        # それ以降の位置引数 (= ターゲット) は捨てる。後段で target_files で差し替える。
        i += 1

    # --commands プレースホルダを当該ツールで埋める。
    replaced: list[str] = []
    j = 0
    commands_replaced = False
    while j < len(filtered):
        arg = filtered[j]
        if arg == "--commands":
            replaced.extend(["--commands", tool])
            commands_replaced = True
            j += 2 if j + 1 < len(filtered) else 1
            continue
        if arg == "--commands=":
            replaced.append(f"--commands={tool}")
            commands_replaced = True
            j += 1
            continue
        if arg.startswith("--commands="):
            replaced.append(f"--commands={tool}")
            commands_replaced = True
            j += 1
            continue
        replaced.append(arg)
        j += 1
    # --commands 未指定だった場合は追記する (サブコマンドの直後に挿入)。
    if not commands_replaced:
        insert_at = 0
        for k, tok in enumerate(replaced):
            if not tok.startswith("-"):
                insert_at = k + 1
                break
        replaced[insert_at:insert_at] = ["--commands", tool]

    # ターゲットを元 cwd 基準の絶対パスに変換して末尾に追加する。
    target_strs: list[str] = []
    for target in target_files:
        if target.is_absolute():
            target_strs.append(str(target))
        else:
            target_strs.append(str((cwd_path / target).resolve(strict=False)))

    parts: list[str] = [*launcher_prefix, *replaced, *target_strs]
    return shlex.join(parts)


# 引数を別トークンで受け取るオプション (= なし形式) の集合。
# retry_command 整形時に位置引数と誤認しないよう明示する。
_VALUE_OPTIONS: frozenset[str] = frozenset(
    {
        "--commands",
        "--output-format",
        "--output-file",
        "--work-dir",
        "-j",
        "--jobs",
    }
)


def _option_takes_value(opt: str) -> bool:
    """オプションが次のトークンを値として取るかを判定する。

    ``--foo=bar`` 形式は判定対象外 (呼び出し側で `=` の有無を先に確認する前提)。
    ビルトイン / カスタムコマンドの ``--{cmd}-args`` も値を伴うため、末尾が
    ``-args`` で終わるものは全て True として扱う。
    """
    if opt.endswith("-args"):
        return True
    return opt in _VALUE_OPTIONS


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

    # --ui と --no-ui の競合チェック
    if args.ui and args.no_ui:
        parser.error("--ui と --no-ui は同時に指定できません。")

    # --version (実行系サブコマンド下でも許容)
    if args.version:
        logger.info(f"pyfltr {importlib.metadata.version('pyfltr')}")
        return 0

    # 構造化出力 stdout モード (CLI で `--output-format=jsonl|sarif|github-annotations`
    # かつ `--output-file` 未指定) は load_config() 前から判定できるため、先行して
    # root logger を抑止する。これにより load_config() 失敗時のエラーログや以降の
    # 警告/エラーが stdout/stderr に漏れない。抑止状態は try/finally で必ず復元する
    # (run() は同一プロセス内で複数回呼ばれる設計)。
    output_format = _resolve_output_format(parser, args.output_format)
    output_file: pathlib.Path | None = args.output_file
    structured_stdout = output_format in _STRUCTURED_OUTPUT_FORMATS and output_file is None
    if structured_stdout:
        _force_structured_stdout_mode(args)
    suppression = _suppress_logging() if structured_stdout else None

    try:
        # pyproject.toml
        try:
            config = pyfltr.config.load_config()
        except (ValueError, OSError) as e:
            if structured_stdout:
                # 抑止済みなので text 出力は出せない。LLM 側は構造化出力 0 行 + exit 非 0 で検知する。
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
            if structured_stdout:
                _force_structured_stdout_mode(args)
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
    finally:
        if suppression is not None:
            _restore_logging(suppression)


def run_pipeline(
    args: argparse.Namespace,
    commands: list[str],
    config: pyfltr.config.Config,
    *,
    original_cwd: str | None = None,
    original_sys_args: list[str] | None = None,
) -> tuple[int, str | None]:
    """実行パイプライン。

    Returns:
        ``(exit_code, run_id)`` のタプル。
        ``exit_code`` は 0 = 成功、1 = 失敗。
        ``run_id`` は実行アーカイブが有効で採番に成功した場合の ULID 文字列、
        無効または採番失敗時は ``None``。
    """
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

    # --only-failed 指定時は直前 run からツール別の失敗ファイル集合を構築する。
    # archive / cache 初期化より前に実行し、早期終了の場合はそれらの副作用を発生させない。
    commands, only_failed_targets, only_failed_exit_early = _apply_only_failed_filter(args, commands, all_files)
    if only_failed_exit_early:
        return 0, None

    # retry_command 再構成用のベース情報を確定する。original_cwd は run() が保存した
    # --work-dir 適用前の cwd、original_sys_args は起動時の sys.argv[1:] のコピー。
    effective_cwd = original_cwd if original_cwd is not None else os.getcwd()
    effective_sys_args = list(original_sys_args) if original_sys_args is not None else list(sys.argv[1:])
    launcher_prefix = _detect_launcher_prefix()
    retry_args_template = _build_retry_args_template(effective_sys_args)

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
        _populate_retry_command(
            result,
            retry_args_template=retry_args_template,
            launcher_prefix=launcher_prefix,
            original_cwd=effective_cwd,
        )

    # UIの判定
    use_ui = not args.no_ui and (args.ui or pyfltr.ui.can_use_ui())

    # 構造化出力 stdout モード: ツール完了時に随時出力行を書き出す (jsonl のみストリーミング)。
    # sarif / github-annotations は全結果集約後に 1 回だけ書き出すため、ストリーミング経路には入れない。
    jsonl_stdout = (args.output_format == "jsonl") and (args.output_file is None)
    structured_stdout = (args.output_format in _STRUCTURED_OUTPUT_FORMATS) and (args.output_file is None)

    if jsonl_stdout:
        pyfltr.llm_output.write_jsonl_header(commands=commands, files=len(all_files), run_id=run_id)

    # 各ツール完了時のフック: retry_command 付与 → archive 書き込み → on_result (ストリーミング)。
    # retry_command は archive と jsonl streaming の双方で必要になるため、archive_hook より前に挿入する。
    composed_hook: typing.Callable[[pyfltr.command.CommandResult], None] | None = None
    if archive_hook is not None:

        def _composed_archive_hook(result: pyfltr.command.CommandResult) -> None:
            _attach_retry_command(result)
            archive_hook(result)

        composed_hook = _composed_archive_hook
    else:
        composed_hook = _attach_retry_command

    # run
    include_fix_stage = bool(getattr(args, "include_fix_stage", False))
    fail_fast = bool(getattr(args, "fail_fast", False))
    if use_ui:
        results, returncode = pyfltr.ui.run_commands_with_ui(
            commands,
            args,
            config,
            all_files,
            archive_hook=composed_hook,
            cache_store=cache_store,
            cache_run_id=run_id,
            fail_fast=fail_fast,
            only_failed_targets=only_failed_targets,
        )
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
            archive_hook=composed_hook,
            cache_store=cache_store,
            cache_run_id=run_id,
            fail_fast=fail_fast,
            only_failed_targets=only_failed_targets,
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
    elif structured_stdout and args.output_format == "sarif":
        _write_sarif_stdout(
            results,
            config,
            exit_code=returncode,
            commands=commands,
            files=len(all_files),
            run_id=run_id,
        )
    elif structured_stdout and args.output_format == "github-annotations":
        _write_github_annotations_stdout(results, config)
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

    return (returncode, run_id)


def _write_sarif_stdout(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
    *,
    exit_code: int,
    commands: list[str],
    files: int,
    run_id: str | None,
) -> None:
    """SARIF 2.1.0 形式の JSON を stdout に書き出す。"""
    import json  # pylint: disable=import-outside-toplevel

    sarif = pyfltr.sarif_output.build_sarif(
        results,
        config,
        exit_code=exit_code,
        commands=commands,
        files=files,
        run_id=run_id,
    )
    sys.stdout.write(json.dumps(sarif, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _write_github_annotations_stdout(
    results: list[pyfltr.command.CommandResult],
    config: pyfltr.config.Config,
) -> None:
    """GitHub Actions 注釈形式の行群を stdout に書き出す。"""
    lines = pyfltr.github_annotations.build_github_annotation_lines(results, config)
    for line in lines:
        sys.stdout.write(line)
        sys.stdout.write("\n")
    sys.stdout.flush()


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
