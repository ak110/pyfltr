"""MCPサーバー本体。

`pyfltr mcp`サブコマンドでstdioトランスポートのMCPサーバーを起動する。
MCPServerを用いて11ツールを公開し、
LLMエージェントがpyfltrの実行と実行アーカイブ参照を直接利用できるようにする。

MCPをコーディングエージェントの常用経路と位置づけ、CLIで可能な操作を原則として露出する。
端末表示と出力先の制御は、MCPが構造化データを戻り値で返すため対象外とする。
`--no-archive`も`run_id`を返す戻り値契約と両立しないため露出しない。
ビルトインツールごとの`--{tool}-args`群は、スキーマの肥大化に見合う用途がないため対象外とする。

引数解決はCLIと共通化する。サブコマンド既定値の注入、カンマ区切りの展開、
未知コマンドの検証には`pyfltr.cli.command_selection`の公開関数を用いる。

動作: `run`ツールは`pyfltr.cli.pipeline.run_pipeline`を直接呼ぶため、
モノレポモード（起点cwd配下に複数の`pyproject.toml`を検出した場合）の
サブプロジェクト分割実行をMCP経由でも自動的に継承する。
公開スキーマには`subproject`識別フィールドを追加しないため、利用者が観測する
レコード構造は単一プロジェクト時と同じになる。

サーバー実装を`mcp_server.py`へ分離し、サードパーティ`mcp`パッケージとの
import衝突を避けながらCLI入口から遅延なく参照できる構成にする。
"""

from __future__ import annotations

import argparse
import importlib.metadata
import logging
import pathlib
import sys
import types as types_module
import typing

import pydantic

# 配布物の版指定は下流プロジェクトの依存解決で上書きされる場合がある。
# MCP専用依存のimport失敗を捕捉し、他サブコマンドとヘルプの起動を維持する。
try:
    import mcp.server.mcpserver as _imported_mcpserver
    import mcp.server.mcpserver.exceptions as _imported_mcp_exceptions
    import mcp.types as _imported_mcp_types

    import pyfltr.cli.mcp_transport as _imported_mcp_transport
except ImportError as e:  # 依存解決が配布物の宣言と異なる環境で到達する。
    _mcpserver: types_module.ModuleType | None = None
    _mcp_exceptions: types_module.ModuleType | None = None
    _mcp_types: types_module.ModuleType | None = None
    _mcp_transport: types_module.ModuleType | None = None
    _MCP_IMPORT_ERROR: ImportError | None = e
else:
    _mcpserver = _imported_mcpserver
    _mcp_exceptions = _imported_mcp_exceptions
    _mcp_types = _imported_mcp_types
    _mcp_transport = _imported_mcp_transport
    _MCP_IMPORT_ERROR = None

import pyfltr.cli.command_info
import pyfltr.cli.command_selection
import pyfltr.cli.mcp_errors
import pyfltr.cli.mcp_grep_tools
import pyfltr.cli.mcp_run_tools
import pyfltr.cli.mcp_runs_tools
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.operations
import pyfltr.warnings_
from pyfltr.cli.mcp_models import (
    CommandInfoModel,
    ConfigResultModel,
)

if typing.TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


# `pydantic.Field`の説明はMCPの`inputSchema`へ載り、モデルが引数を決める唯一の手掛かりとなる。
# docstringはクライアントへ送られないため、意味・省略時の動作・組み合わせ条件は説明へ書く。


COMMAND_INFO_EXCLUDED_KEYS: frozenset[str] = frozenset({"command", "resolved"})
"""`command_info`の`info`から除くキー。応答最上位の`command`・`resolved`と重複するため。

`info`はCLIの`command-info`と共通の解決結果の辞書で、起動方式の解決失敗を切り分けるための項目を持つ。
キーを加える場合は`tests/cli/mcp_models_test.py`の`_COMMAND_INFO_KEY_PURPOSES`へ用途を書く。
"""


# ---------------------------------------------------------------------------
# MCPServerツール関数群（公開名は@mcp.tool(name=...)で明示）
# ---------------------------------------------------------------------------

# build_server()内で登録するため、ここではデコレーターを付けない。
# 公開名はbuild_server()で@mcp.tool(name="...")によって明示的に設定する。
# 公開名はアンダースコア区切り（`list_runs`等）を採用する。CLIサブコマンドの
# ハイフン形式（`list-runs`）とは異なるが、`@mcp.tool()`のスキーマ名規則上
# ハイフンは非推奨で互換性のあるMCPServer経路もアンダースコア前提のため。


async def tool_command_info(
    command: typing.Annotated[str, pydantic.Field(description="対象のコマンド名（`ruff-check`など）。")],
    check: typing.Annotated[
        bool,
        pydantic.Field(
            description=(
                "Trueで実行時と同じ事前確認を行う。`mise install`・`mise trust`の試行や"
                "パッケージマネージャーの版確認などの副作用が起こりうる。既定はFalseで副作用を起こさない。"
            ),
        ),
    ] = False,
) -> CommandInfoModel:
    """ツールの起動方式の解決結果を返す。

    対応CLI: `pyfltr command-info <command> [--check]`
    """
    pyfltr.warnings_.clear()
    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            f"設定エラー: {pyfltr.config.config.format_config_error(exc, retry='`command_info`ツール')}"
        )
    try:
        pyfltr.cli.command_selection.validate_commands(
            [command], config, list_commands=pyfltr.cli.command_selection.MCP_LIST_COMMANDS
        )
    except ValueError as exc:
        pyfltr.cli.mcp_errors.raise_mcp_error(str(exc))
    info = pyfltr.cli.command_info.collect_info(command, config, do_check=check)
    return CommandInfoModel(
        command=command,
        resolved=bool(info.get("resolved", True)),
        info={key: value for key, value in info.items() if key not in COMMAND_INFO_EXCLUDED_KEYS},
    )


async def tool_config(
    action: typing.Annotated[
        typing.Literal["get", "set", "delete", "list"],
        pydantic.Field(
            description=(
                "操作。`get`は`key`の値を返す（`key`必須、`value`不可）。"
                "`set`は`key`へ`value`を書き込む（両方必須）。`delete`は`key`を削除する（`key`必須、`value`不可）。"
                "`list`は設定値を一覧する（`key`・`value`不可）。`set`・`delete`は設定ファイルを書き換える。"
            ),
        ),
    ],
    key: typing.Annotated[str | None, pydantic.Field(description="`get`・`set`・`delete`で必須の設定キー名。")] = None,
    value: typing.Annotated[str | None, pydantic.Field(description="`set`で必須の設定値。キーの型に合わせて変換する。")] = None,
    use_global: typing.Annotated[
        bool,
        pydantic.Field(description="Trueでグローバル設定ファイルを対象にする。既定はサーバーのcwdにある`pyproject.toml`。"),
    ] = False,
    include_defaults: typing.Annotated[
        bool, pydantic.Field(description='`action="list"`で、既定値のままのキーも含める。')
    ] = False,
) -> ConfigResultModel:
    """pyfltr設定ファイルを操作する。

    対応CLI: `pyfltr config get|set|delete|list`
    """
    pyfltr.warnings_.clear()
    if action not in ("get", "set", "delete", "list"):
        pyfltr.cli.mcp_errors.raise_mcp_error("action は get / set / delete / list のいずれかを指定してください。")
    if action in ("get", "delete"):
        if key is None:
            pyfltr.cli.mcp_errors.raise_mcp_error(f'action="{action}"では key を指定してください。')
        if value is not None:
            pyfltr.cli.mcp_errors.raise_mcp_error(
                f'action="{action}"では value を指定できません。valueを省略して呼び直してください。'
            )
    elif action == "set":
        if key is None or value is None:
            pyfltr.cli.mcp_errors.raise_mcp_error('action="set"では key と value を指定してください。')
    elif key is not None or value is not None:
        pyfltr.cli.mcp_errors.raise_mcp_error(
            'action="list"では key と value を指定できません。keyとvalueを省略して呼び直してください。'
        )
    if include_defaults and action != "list":
        pyfltr.cli.mcp_errors.raise_mcp_error('include_defaults は action="list"のときのみ指定できます。')

    path = pyfltr.config.model.default_global_config_path() if use_global else pathlib.Path("pyproject.toml").absolute()
    result = pyfltr.config.operations.execute(
        pyfltr.config.operations.ConfigRequest(
            action=action,
            path=path,
            key=key,
            value=value,
            use_global=use_global,
            include_defaults=include_defaults,
            global_option="`use_global=True`",
            retry_operation="`config`ツール",
        )
    )
    if result.error is not None:
        pyfltr.cli.mcp_errors.raise_mcp_error(result.error)
    if action == "get":
        return ConfigResultModel(action=action, path=str(path), key=key, value=result.value, is_default=result.is_default)
    if action == "set":
        return ConfigResultModel(action=action, path=str(path), key=key, value=result.value, warnings=result.warnings)
    if action == "delete":
        return ConfigResultModel(action=action, path=str(path), key=key, existed=result.existed)
    return ConfigResultModel(action=action, path=str(path), values=result.values)


# ---------------------------------------------------------------------------
# MCPServer組み立て
# ---------------------------------------------------------------------------

# 初期化応答でクライアントへ渡すサーバー全体の説明。ツール検索が有効なホストでは
# ツール名とこの説明だけが先に読まれるため、扱う作業とツールの選び方を書く。
SERVER_INSTRUCTIONS = (
    "pyfltrは多言語のformatter・linter・testerとプロジェクト固有のチェックを、設定に従って一括実行する。"
    "チェックの実行は`run`を使い、`mode`で`run`（自動修正あり）・`fast`（`fast`設定が有効なツールのみ）・`ci`（fixステージなし、formatterによる書き換えを失敗扱い）を選ぶ。"
    "実行結果は実行アーカイブに保存され、`list_runs`で一覧、`show_run`で概要、"
    "`show_run_diagnostics`で診断、`show_run_output`でツールの生出力を参照する。"
    "pyfltrの除外設定に従うファイル横断の検索は`grep`、置換は`replace`（既定はdry run）を使い、"
    "取り消しと履歴は`replace_undo`・`replace_history`で扱う。"
    "pyfltrの設定の参照と変更は`config`、各ツールの起動方式の確認は`command_info`を使う。"
)

TOOL_HANDLERS = {
    handler.__name__: handler
    for handler in (
        pyfltr.cli.mcp_runs_tools.tool_list_runs,
        pyfltr.cli.mcp_runs_tools.tool_show_run,
        pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics,
        pyfltr.cli.mcp_runs_tools.tool_show_run_output,
        pyfltr.cli.mcp_run_tools.tool_run,
        pyfltr.cli.mcp_grep_tools.tool_grep,
        pyfltr.cli.mcp_grep_tools.tool_replace,
        pyfltr.cli.mcp_grep_tools.tool_replace_undo,
        pyfltr.cli.mcp_grep_tools.tool_replace_history,
        tool_command_info,
        tool_config,
    )
}


def build_server() -> MCPServer:
    """MCPServerインスタンスを生成し、11ツールを登録して返す。

    公開名は`@mcp.tool(name=...)`で明示し、Python側の関数名（`tool_*`）
    とは独立したスキーマ名（`list_runs`等）を維持する。

    `version`は初期化応答の`serverInfo.version`としてクライアントへ返る
    MCPプロトコルの必須フィールドであり、省略するとSDKの既定値である空文字列が入る。
    値は`importlib.metadata`から取得し、JSONL headerレコード・SARIF出力と取得元を揃える
    （実行中の版と配布物の版を一致させるため）。
    """
    if _mcpserver is None:
        raise RuntimeError("MCPサーバー機能に必要な依存を読み込めません") from _MCP_IMPORT_ERROR
    assert _mcp_transport is not None and _mcp_types is not None
    mcp = _mcpserver.MCPServer("pyfltr", version=importlib.metadata.version("pyfltr"), instructions=SERVER_INSTRUCTIONS)
    read_only = _mcp_types.ToolAnnotations(read_only_hint=True)
    # 対象ファイルや設定ファイルを上書きしうるツール。ホストへ確認を促す。
    writes_files = _mcp_types.ToolAnnotations(destructive_hint=True)
    for name, title, description, tool_annotations, handler in (
        (
            "list_runs",
            "実行履歴の一覧",
            "実行アーカイブに保存されたrunの一覧を新しい順に返す。最新runだけを見る場合は`show_run`へ`latest`を渡す。"
            "`latest`は作業ディレクトリによらず最新のrunを指す。",
            read_only,
            pyfltr.cli.mcp_runs_tools.tool_list_runs,
        ),
        (
            "show_run",
            "実行結果の概要",
            "指定runのmeta情報、経過時間の要約（`timing`）、コマンド別サマリ（状態・診断件数・所要時間・本体区間の時刻）を返す。"
            "`timing`は他の検証の終了を待った区間（`slot_waits`）とrun全体の経過を持ち、未確定の値はnullとする。"
            "診断の本文は`show_run_diagnostics`、ツールの生出力は`show_run_output`で取得する。",
            read_only,
            pyfltr.cli.mcp_runs_tools.tool_show_run,
        ),
        (
            "show_run_diagnostics",
            "実行結果の診断",
            "指定run・コマンドの診断（ファイル単位に集約した指摘）とコマンドのmeta情報を返す。"
            "meta情報は検査対象ファイルの引数列を含まない。"
            "既定は全件を返す。件数が多い場合は`offset`・`limit`で個別指摘の範囲を指定して分割取得でき、"
            "応答の`total_diagnostics`と`next_offset`で続きを判断できる。",
            read_only,
            pyfltr.cli.mcp_runs_tools.tool_show_run_diagnostics,
        ),
        (
            "show_run_output",
            "実行結果の出力ログ",
            "指定run・コマンドのツールの生出力（output.log）を返す。診断として解析された指摘は`show_run_diagnostics`で取得する。"
            "既定は全文を返す。出力が大きい場合は`offset`・`limit`で行範囲を指定して分割取得でき、"
            "各値が`text`・`total_lines`・`next_offset`を持つオブジェクトになる。",
            read_only,
            pyfltr.cli.mcp_runs_tools.tool_show_run_output,
        ),
        (
            "run",
            "チェックの実行",
            "指定パスに対してformatter・linter・testerを実行し、完了判定・失敗コマンド・run_idなどを返す。"
            'formatterと`mode="run"`・`"fast"`のfixステージは対象ファイルを書き換える。'
            " 応答は`completion`を最初に読み、`completed`以外なら`incomplete_commands`・`missing_targets`・"
            "`fully_excluded_files`・`skipped_reason`・`warnings`で理由を確認する。"
            "`failed`が空でなければ`show_run_diagnostics`で診断を取得する。"
            " 戻り値の`retry_commands`は失敗コマンドを再実行するシェルコマンドで、"
            "`only_failed=True`でも失敗したツールだけを再実行できる。",
            writes_files,
            pyfltr.cli.mcp_run_tools.tool_run,
        ),
        (
            "grep",
            "ファイル横断の検索",
            "ファイル群から正規表現（`fixed_strings=True`で固定文字列）を検索し、一致一覧を返す。"
            "pyfltr設定の`exclude`と`.gitignore`に従って対象を選ぶ。ファイルは変更しない。"
            " 大きな結果は既定で縮約し、縮約時は`output_mode`と`omitted_matches`で省略を示す。"
            "置換は`replace`を使う。",
            read_only,
            pyfltr.cli.mcp_grep_tools.tool_grep,
        ),
        (
            "replace",
            "ファイル横断の置換",
            "ファイル群へ正規表現置換を適用する。既定の`dry_run=True`では変更内容だけを返してファイルを変更しない。"
            "`dry_run=False`ではファイルを書き換え、取り消し用の`replace_id`と履歴を保存する。"
            "置換前の確認には`grep`、取り消しには`replace_undo`を使う。",
            writes_files,
            pyfltr.cli.mcp_grep_tools.tool_replace,
        ),
        (
            "replace_undo",
            "置換の取り消し",
            "`replace`で書き換えたファイルを、`replace_id`の履歴から変更前の内容へ書き戻す。"
            "replace後に編集されたファイルは既定で書き戻さず`skipped`へ挙げ、`force=True`で強制的に書き戻す。",
            writes_files,
            pyfltr.cli.mcp_grep_tools.tool_replace_undo,
        ),
        (
            "replace_history",
            "置換履歴の参照",
            '`replace`の履歴を一覧（`action="list"`）または1件（`action="show"`）で返す。ファイルは変更しない。',
            read_only,
            pyfltr.cli.mcp_grep_tools.tool_replace_history,
        ),
        (
            "command_info",
            "ツール起動方式の確認",
            "コマンドの起動方式（runner・実行ファイル・最終コマンドラインなど）の解決結果を返す。"
            "既定は副作用を起こさない。`check=True`は`mise install`・`mise trust`の試行や版確認の副作用を伴う。",
            # `check=True`はmiseの導入・信頼設定を変えうるため読み取り専用とせず、上書きもしないため破壊的ともしない。
            _mcp_types.ToolAnnotations(read_only_hint=False, destructive_hint=False),
            tool_command_info,
        ),
        (
            "config",
            "設定の参照と変更",
            "pyfltrの設定を取得・一覧（`get`・`list`）し、変更・削除（`set`・`delete`）する。"
            "`set`・`delete`は`pyproject.toml`（`use_global=True`ではグローバル設定ファイル）を書き換える。",
            writes_files,
            tool_config,
        ),
    ):
        mcp.tool(name=name, title=title, description=description, annotations=tool_annotations)(
            _mcp_transport.isolate_tool(handler, pyfltr.cli.mcp_errors.raise_mcp_error)
        )

    return mcp


# ---------------------------------------------------------------------------
# サブコマンド登録・エントリポイント
# ---------------------------------------------------------------------------


def execute_mcp(args: argparse.Namespace) -> int:
    """`mcp`サブコマンドの処理本体。

    stdioトランスポートでMCPサーバーを起動する。
    起動直後にroot loggerをstderrへ向けてJSON-RPCフレームのstdout汚染を防ぐ。
    MCPServerの`run(transport="stdio")`はstdin EOFで終了する。
    """
    del args  # サブコマンド呼び出し規約上受け取るのみ（mcpは追加引数を持たない）

    # stdioトランスポートではstdoutをJSON-RPCフレームが専有するため、
    # ロギングは必ずstderrへ向ける。
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")

    if _mcpserver is None:
        logger.error(
            "MCPサーバー機能に必要な依存が解決されていません。"
            "`uv tool install --reinstall pyfltr` でMCP用の環境を再導入し、"
            "クライアントのMCPサーバーを再起動してください: %s",
            _MCP_IMPORT_ERROR,
        )
        return 1

    try:
        server = build_server()
        server.run(transport="stdio")
        return 0
    except Exception as e:  # MCPサーバー起動失敗をエージェント側へ非ゼロ終了で通知するため全例外を捕捉する
        logger.error(
            "MCP サーバーの起動に失敗しました: %s。"
            "https://ak110.github.io/pyfltr/guide/troubleshooting/ のMCP関連の節で切り分け手順を確認してください",
            e,
        )
        return 1
