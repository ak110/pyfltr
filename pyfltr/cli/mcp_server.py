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

動作: `run_for_agent`は`pyfltr.cli.pipeline.run_pipeline`を直接呼ぶため、
モノレポモード（起点cwd配下に複数の`pyproject.toml`を検出した場合）の
サブプロジェクト分割実行をMCP経由でも自動的に継承する。
公開スキーマには`subproject`識別フィールドを追加しないため、利用者が観測する
レコード構造は単一プロジェクト時と同じになる。

サーバー実装を`mcp_server.py`へ分離し、サードパーティ`mcp`パッケージとの
import衝突を避けながらCLI入口から遅延なく参照できる構成にする。
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import pathlib
import sys
import tempfile

# 本モジュールは`types`をgrep系ツール関数の引数名に使うため、標準ライブラリ側を別名で取り込む。
# 同名のままではモジュール名を引数が覆い、pylintの`redefined-outer-name`に抵触する。
import types as types_module
import typing

# 配布物の版指定は下流プロジェクトの依存解決で上書きされる場合がある。
# MCP専用依存のimport失敗を捕捉し、他サブコマンドとヘルプの起動を維持する。
try:
    import mcp.server.mcpserver as _imported_mcpserver
except ImportError as e:  # 依存解決が配布物の宣言と異なる環境で到達する。
    _mcpserver: types_module.ModuleType | None = None
    _MCP_IMPORT_ERROR: ImportError | None = e
else:
    _mcpserver = _imported_mcpserver
    _MCP_IMPORT_ERROR = None

import pyfltr.cli.command_info
import pyfltr.cli.command_selection
import pyfltr.cli.overrides
import pyfltr.cli.pipeline
import pyfltr.cli.replace_subcmd
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.grep_.history
import pyfltr.grep_.matcher
import pyfltr.grep_.replacer
import pyfltr.grep_.scanner
import pyfltr.paths
import pyfltr.state.archive
import pyfltr.state.runs
import pyfltr.warnings_
from pyfltr.cli.mcp_models import (
    CommandDiagnosticsModel,
    CommandInfoModel,
    CommandSummaryModel,
    ConfigResultModel,
    DiagnosticMessageModel,
    DiagnosticModel,
    GrepFileCountModel,
    GrepMatchModel,
    GrepResultModel,
    ReplaceChangeRecordModel,
    ReplaceFileChangeModel,
    ReplaceHistoryEntryModel,
    ReplaceHistoryFileModel,
    ReplaceHistoryModel,
    ReplaceResultModel,
    ReplaceUndoModel,
    RunForAgentResult,
    RunOverviewModel,
    RunSummaryModel,
)
from pyfltr.grep_.types import MatchRecord, ReplaceCommandMeta

if typing.TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# エラー変換ヘルパー
# ---------------------------------------------------------------------------


def _raise_mcp_error(msg: str) -> typing.Never:
    """MCPクライアントへエラーとして返すための例外を送出する。

    MCPServerは`ValueError`をツールエラーとしてJSON-RPCエラーレスポンスに変換する。
    """
    raise ValueError(msg)


def _resolve_run_id_or_raise(store: pyfltr.state.archive.ArchiveStore, raw: str) -> str:
    """`resolve_run_id`の結果を返し、エラー時はMCPエラーへ変換する。"""
    try:
        return pyfltr.state.runs.resolve_run_id(store, raw)
    except pyfltr.state.runs.RunIdError as e:
        _raise_mcp_error(str(e))


# ---------------------------------------------------------------------------
# MCPServerツール関数群（公開名は@mcp.tool(name=...)で明示）
# ---------------------------------------------------------------------------

# build_server()内で登録するため、ここではデコレーターを付けない。
# 公開名はbuild_server()で@mcp.tool(name="...")によって明示的に設定する。
# 公開名はアンダースコア区切り（`list_runs`等）を採用する。CLIサブコマンドの
# ハイフン形式（`list-runs`）とは異なるが、`@mcp.tool()`のスキーマ名規則上
# ハイフンは非推奨で互換性のあるMCPServer経路もアンダースコア前提のため。


async def tool_list_runs(limit: int = 20) -> list[RunSummaryModel]:
    """実行アーカイブに保存されたrun一覧を新しい順で返す。

    対応CLI: `pyfltr list-runs`
    """
    store = pyfltr.state.archive.ArchiveStore()
    summaries = store.list_runs(limit=limit)
    return [
        RunSummaryModel(
            run_id=s.run_id,
            started_at=s.started_at,
            finished_at=s.finished_at,
            exit_code=s.exit_code,
            commands=list(s.commands),
            files=s.files,
        )
        for s in summaries
    ]


async def tool_show_run(run_id: str) -> RunOverviewModel:
    """指定runのmeta情報とコマンド別サマリを返す。

    `run_id`はULID完全一致・前方一致・`latest`エイリアスを受け付ける。

    対応CLI: `pyfltr show-run <run_id>`
    """
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    try:
        meta = store.read_meta(resolved)
    except FileNotFoundError:
        _raise_mcp_error(f"run_id が見つかりません: {resolved}")
    command_summaries = pyfltr.state.runs.collect_tool_summaries(store, resolved)
    commands = [
        CommandSummaryModel(
            command=entry.get("command"),
            status=entry.get("status"),
            has_error=entry.get("has_error"),
            diagnostics=entry.get("diagnostics"),
        )
        for entry in command_summaries
    ]
    return RunOverviewModel(run_id=resolved, meta=meta, commands=commands)


async def tool_show_run_diagnostics(run_id: str, commands: list[str]) -> list[CommandDiagnosticsModel]:
    """指定run・コマンドのtool.jsonとdiagnostics.jsonl全件を返す。

    `diagnostics`は`(command, file)`単位の集約形式で、個別指摘は`messages`に並ぶ。
    rule→URL辞書`hint_urls`はtool.json由来でそのまま返す。
    `commands`に複数を指定すると、要素ごとの結果を入力順で返す。

    対応CLI: `pyfltr show-run <run_id> --commands <name1>,<name2>`
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    results: list[CommandDiagnosticsModel] = []
    for command in commands:
        try:
            command_meta = store.read_tool_meta(resolved, command)
            diagnostics_raw = store.read_tool_diagnostics(resolved, command)
        except FileNotFoundError:
            _raise_mcp_error(f"run {resolved} にコマンド {command!r} の結果が保存されていません。")
        diagnostics = [
            DiagnosticModel(
                command=d.get("command", d.get("tool")),
                file=d.get("file"),
                messages=[DiagnosticMessageModel(**m) for m in d.get("messages", [])],
            )
            for d in diagnostics_raw
        ]
        hint_urls = command_meta.get("hint_urls") if isinstance(command_meta.get("hint_urls"), dict) else None
        hints = command_meta.get("hints") if isinstance(command_meta.get("hints"), dict) else None
        results.append(
            CommandDiagnosticsModel(command_meta=command_meta, diagnostics=diagnostics, hint_urls=hint_urls, hints=hints)
        )
    return results


async def tool_show_run_output(run_id: str, commands: list[str]) -> dict[str, str]:
    """指定run・コマンドのoutput.log全文を返す。

    戻り値はコマンド名→全文の辞書。`commands`に複数を指定すると入力順で各全文を返す。

    対応CLI: `pyfltr show-run <run_id> --commands <name> --output`（単一指定のみ）
    """
    if not commands:
        _raise_mcp_error("commands を 1 件以上指定してください。")
    store = pyfltr.state.archive.ArchiveStore()
    resolved = _resolve_run_id_or_raise(store, run_id)
    outputs: dict[str, str] = {}
    for command in commands:
        try:
            outputs[command] = store.read_tool_output(resolved, command)
        except FileNotFoundError:
            _raise_mcp_error(f"run {resolved} にコマンド {command!r} の結果が保存されていません。")
    return outputs


async def tool_run_for_agent(
    paths: list[str],
    mode: str = "run",
    commands: list[str] | None = None,
    enable: list[str] | None = None,
    disable: list[str] | None = None,
    exclude_fence_under: list[str] | None = None,
    no_fix: bool = False,
    fail_fast: bool = False,
    only_failed: bool = False,
    from_run: str | None = None,
    changed_since: str | None = None,
    work_dir: str | None = None,
    allow_external_paths: bool = False,
    no_exclude: bool = False,
    no_gitignore: bool = False,
    no_cache: bool = False,
    human_readable: bool = False,
    shuffle: bool = False,
    exit_zero_even_if_formatted: bool = False,
    jobs: int | None = None,
) -> RunForAgentResult:
    """指定パスに対してlint/format/testを実行し、結果を返す。

    `run`・`fast`・`ci`の各実行モードをCLIと同じ既定値で扱う。
    実行アーカイブは常に有効化され、`run_id`を戻り値に含む。
    early exit（直前runなし・失敗ツールなし・対象ファイル交差が空）の場合は
    `run_id=None`・`skipped_reason`に理由を設定して返す。

    対応CLI: `pyfltr run` / `pyfltr fast` / `pyfltr ci`

    Args:
        paths: 実行対象のファイルまたはディレクトリのパス一覧。
        mode: 実行モード。`run`はfixステージを有効化し、formatter変更を成功扱いにする。
            `fast`はfast設定が有効なツールだけを対象にする。`ci`はfixステージを無効化し、
            formatter変更を失敗扱いにする。
        commands: 実行するコマンド名のリスト。省略時はプロジェクト設定の全コマンドを使用する。
        enable: 一時的に有効化するコマンド名のリスト。カンマ区切りも受理する。
        disable: 一時的に無効化するコマンド名のリスト。カンマ区切りも受理する。
        exclude_fence_under: フェンス内側を検査対象から除外するH2見出しのリスト。
        no_fix: Trueの場合、`run`と`fast`のfixステージを抑止する。`ci`は元から無効となる。
        fail_fast: Trueの場合、1ツールでもエラーが発生した時点で残りを打ち切る。
        only_failed: Trueの場合、直前runの失敗ツール・失敗ファイルのみ再実行する。
        from_run: `only_failed=True`時の参照run_id（前方一致・`latest`可）。
            `only_failed=False`かつ`from_run`指定はValueError。
        changed_since: 指定したgit参照から変更されたファイルだけを対象にする。
        work_dir: 実行の起点ディレクトリ。設定探索と相対パス解決の基準を兼ねる。
            省略時はMCPサーバープロセスのカレントディレクトリを用いる。
            指定時は診断のファイルパスを絶対パスで返す場合がある。
        allow_external_paths: Trueの場合、実行起点の外側にあるパスを許可する。
        no_exclude: Trueの場合、設定の除外パターンを無効化する。
        no_gitignore: Trueの場合、`.gitignore`による除外を無効化する。
        no_cache: Trueの場合、ファイルhashキャッシュを無効化する。
        human_readable: Trueの場合、対応ツールの機械可読出力を抑止する。
        shuffle: Trueの場合、実行対象ファイルの順序をシャッフルする。
        exit_zero_even_if_formatted: Trueの場合、formatterによる変更だけなら成功扱いにする。
        jobs: 並列実行するツール数の上限。
    """
    if mode not in ("run", "fast", "ci"):
        _raise_mcp_error("mode は run / fast / ci のいずれかを指定してください。")
    if from_run is not None and not only_failed:
        _raise_mcp_error("from_run は only_failed=True のときのみ指定できます。")

    work_dir_path = pathlib.Path(work_dir).expanduser().resolve() if work_dir is not None else None
    if work_dir_path is not None and not work_dir_path.is_dir():
        _raise_mcp_error(f"work_dir が存在するディレクトリではありません: {work_dir}")

    base = work_dir_path if work_dir_path is not None else pathlib.Path.cwd()
    targets = [path if (path := pathlib.Path(raw)).is_absolute() else base / path for raw in paths]

    args = argparse.Namespace(
        targets=targets,
        # CLI経路（`--commands`はaction="append"）と同じ`list[str] | None`で保持する。
        commands=list(commands) if commands else None,
        enable=list(enable) if enable else None,
        disable=list(disable) if disable else None,
        exclude_fence_under=list(exclude_fence_under) if exclude_fence_under else None,
        no_fix=no_fix,
        fail_fast=fail_fast,
        only_failed=only_failed,
        from_run=from_run,
        changed_since=changed_since,
        no_archive=False,  # アーカイブ必須化のため明示的にFalse
        no_cache=no_cache,
        verbose=False,
        output_format="jsonl",
        output_file=None,  # 後で一時ファイルで上書きする
        ui=None,
        no_ui=True,
        no_clear=True,
        stream=False,
        shuffle=False,
        keep_ui=False,
        ci=mode == "ci",
        human_readable=human_readable,
        no_exclude=no_exclude,
        no_gitignore=no_gitignore,
        allow_external_paths=allow_external_paths,
        jobs=jobs,
        work_dir=work_dir_path,
        exit_zero_even_if_formatted=False,
        version=False,
        subcommand=mode,
        # MCPの戻り値は実行アーカイブから組み立てるためJSONL縮約の影響を受けない。
        # quiet=Trueはstderrへのprecommitガイダンス抑止のみに作用する。
        quiet=True,
    )
    pyfltr.cli.command_selection.apply_subcommand_defaults(args)
    args.shuffle = shuffle
    if exit_zero_even_if_formatted:
        args.exit_zero_even_if_formatted = True
    if no_fix:
        args.include_fix_stage = False

    retry_sys_args = [mode]
    if work_dir_path is not None:
        retry_sys_args.append(f"--work-dir={work_dir_path}")
    if no_fix:
        retry_sys_args.append("--no-fix")
    if commands:
        retry_sys_args.append("--commands=" + ",".join(commands))
    for name in enable or []:
        retry_sys_args.append(f"--enable={name}")
    for name in disable or []:
        retry_sys_args.append(f"--disable={name}")
    for heading in exclude_fence_under or []:
        retry_sys_args.append(f"--exclude-fence-under={heading}")
    if allow_external_paths:
        retry_sys_args.append("--allow-external-paths")
    if no_exclude:
        retry_sys_args.append("--no-exclude")
    if no_gitignore:
        retry_sys_args.append("--no-gitignore")
    if no_cache:
        retry_sys_args.append("--no-cache")
    if human_readable:
        retry_sys_args.append("--human-readable")
    if shuffle:
        retry_sys_args.append("--shuffle")
    if exit_zero_even_if_formatted:
        retry_sys_args.append("--exit-zero-even-if-formatted")
    if jobs is not None:
        retry_sys_args.append(f"--jobs={jobs}")

    # 構造化出力を一時ファイルへ誘導してstdout汚染を防ぐ。
    # NamedTemporaryFileをコンテキストマネージャーで使い、close後もパスを残す（delete=False）。
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        tmp_path = pathlib.Path(tmp.name)

    # MCPのstdoutはJSON-RPCフレームが占有するため、text_loggerはrun_pipeline側で
    # stderrに強制する（force_text_on_stderr=True）。
    # 構造化出力は一時ファイル経由（FileHandler）となりstdoutを汚染しない。
    args.output_file = tmp_path
    try:
        config = pyfltr.config.config.load_config(config_dir=work_dir_path)
        # アーカイブを強制有効化する。MCPツールはrun_idを返す契約を保証する。
        config.values["archive"] = True
        pyfltr.cli.overrides.apply_cli_overrides(config, args)

        commands_list: list[str] = pyfltr.config.config.resolve_aliases(
            pyfltr.cli.command_selection.flatten_commands_arg(args.commands, config), config
        )
        try:
            pyfltr.cli.command_selection.validate_commands(commands_list, config)
        except ValueError as exc:
            _raise_mcp_error(str(exc))

        exit_code, run_id = pyfltr.cli.pipeline.run_pipeline(
            args,
            commands_list,
            config,
            start_cwd=work_dir_path,
            original_cwd=str(work_dir_path) if work_dir_path is not None else None,
            original_sys_args=retry_sys_args,
            force_text_on_stderr=True,
        )
    finally:
        # 一時ファイルを削除する（存在しない場合はそのまま無視する）
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)

    # only_failedによるearly exit: run_idがNoneのとき実行がスキップされた。
    if run_id is None:
        return RunForAgentResult(
            run_id=None,
            exit_code=exit_code,
            failed=[],
            commands=[],
            skipped_reason=(
                "only_failed が有効ですが実行対象がありませんでした（直前 run なし・失敗ツールなし・対象ファイル交差なし）。"
            ),
            retry_commands={},
        )

    # コマンド別サマリを最新アーカイブから集計する。
    store = pyfltr.state.archive.ArchiveStore()
    try:
        command_summaries = pyfltr.state.runs.collect_tool_summaries(store, run_id)
    except Exception:  # MCPツール戻り値の組み立て継続を優先するため全例外を吸収する
        command_summaries = []

    commands_model = [CommandSummaryModel.model_validate(entry) for entry in command_summaries]
    failed_commands = [c.command for c in commands_model if c.has_error and c.command]

    # 失敗コマンドのretry_commandをアーカイブから収集する（F7）。
    retry_commands: dict[str, str] = {}
    for summary_entry in command_summaries:
        cmd_name = summary_entry.get("command")
        if summary_entry.get("has_error") and cmd_name:
            try:
                tool_meta = store.read_tool_meta(run_id, cmd_name)
                rc = tool_meta.get("retry_command")
                if rc:
                    retry_commands[cmd_name] = rc
            except Exception:  # tool.json読み取り失敗は非致命的
                logger.debug("retry_command取得失敗: command=%s", cmd_name, exc_info=True)

    return RunForAgentResult(
        run_id=run_id,
        exit_code=exit_code,
        failed=failed_commands,
        commands=commands_model,
        retry_commands=retry_commands,
    )


async def tool_grep(
    paths: list[str],
    pattern: str | None = None,
    patterns: list[str] | None = None,
    pattern_file: str | None = None,
    ignore_case: bool = False,
    smart_case: bool = False,
    fixed_strings: bool = False,
    word_regexp: bool = False,
    line_regexp: bool = False,
    multiline: bool = False,
    before_context: int = 0,
    after_context: int = 0,
    context: int | None = None,
    max_count: int = 0,
    max_total: int | None = None,
    summary_mode: str | None = None,
    types: list[str] | None = None,
    globs: list[str] | None = None,
    encoding: str = "utf-8",
    max_filesize: int | None = None,
    no_exclude: bool = False,
    no_gitignore: bool = False,
) -> GrepResultModel:
    """指定ファイル群から正規表現パターンを検索し、マッチ一覧を返す。

    pyfltrの`exclude`/`extend-exclude`/`respect-gitignore`設定を尊重する。
    通常検索で未指定の`max_total`は1000とし、CLI既定の無制限より安全側に設定する。

    Args:
        paths: 検索対象のファイルまたはディレクトリパスの一覧。
        pattern: 検索パターン。`patterns`または`pattern_file`のみを指定する場合は省略できる。
        patterns: 追加の検索パターン一覧。`pattern`と連結してOR条件で検索する。
        pattern_file: 1行1パターンのパターンファイルパス。
        ignore_case: 大文字小文字を区別しない。
        smart_case: パターンに大文字を含まない場合のみignore_caseを有効化する。
        fixed_strings: パターンを固定文字列として扱う。
        word_regexp: 単語境界で囲まれたマッチのみ採用する。
        line_regexp: 行全体に一致したマッチのみ採用する。
        multiline: マルチラインマッチを有効化する。
        before_context: マッチ行の前に含める行数。
        after_context: マッチ行の後に含める行数。
        context: `before_context`と`after_context`の一括指定。個別指定が0の方向だけへ適用する。
        max_count: ファイル単位の最大マッチ件数（0で無制限）。
        max_total: 全体の最大マッチ件数。未指定時は通常検索で1000、集計モードで無制限。
            0を明示した場合は常に無制限となる。
        summary_mode: 集計モード。`files_with_matches`、`count`、`files_without_match`のいずれか。
            指定時は`matches`を空で返し、対応する集計フィールドを返す。
            `files_without_match`では正の`max_total`を併用できない。
        types: 対象言語タイプの一覧（例: ["python", "ts"]）。
        globs: globパターンでの対象限定一覧。
        encoding: ファイル読み込み時のエンコーディング（既定: utf-8）。
        max_filesize: 走査対象ファイルサイズの上限（バイト単位）。
        no_exclude: exclude/extend-excludeによる除外を無効化する。
        no_gitignore: .gitignoreによる除外を無効化する。
    """
    # warnings_はモジュールグローバルに蓄積するため、リクエスト開始時に初期化する
    pyfltr.warnings_.clear()
    collected = ([pattern] if pattern is not None else []) + list(patterns or [])
    if pattern_file is not None:
        try:
            collected.extend(pyfltr.grep_.matcher.read_pattern_file(pathlib.Path(pattern_file)))
        except OSError as exc:
            _raise_mcp_error(f"パターンファイルの読み込みに失敗しました: {exc}")
    try:
        compiled = pyfltr.grep_.matcher.compile_pattern(
            collected,
            fixed_strings=fixed_strings,
            ignore_case=ignore_case,
            smart_case=smart_case,
            word_regexp=word_regexp,
            line_regexp=line_regexp,
            multiline=multiline,
        )
    except ValueError as exc:
        _raise_mcp_error(str(exc))

    after_ctx = after_context
    before_ctx = before_context
    if context is not None:
        if after_ctx == 0:
            after_ctx = context
        if before_ctx == 0:
            before_ctx = context

    valid_summary_modes = ("files_with_matches", "count", "files_without_match")
    if summary_mode is not None and summary_mode not in valid_summary_modes:
        _raise_mcp_error("summary_mode は files_with_matches / count / files_without_match のいずれかを指定してください。")
    if summary_mode == "files_without_match" and max_total is not None and max_total > 0:
        _raise_mcp_error("summary_mode=files_without_match では max_total に正の値を指定できません。")
    effective_max_total = (0 if summary_mode is not None else 1000) if max_total is None else max_total

    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        _raise_mcp_error(f"設定エラー: {exc}")

    if no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if no_gitignore:
        config.values["respect-gitignore"] = False

    expanded = pyfltr.command.targets.expand_all_files(
        [pathlib.Path(p) for p in paths],
        config,
    )
    expanded = pyfltr.grep_.scanner.filter_files_by_type(expanded, types or [])
    expanded = pyfltr.grep_.scanner.filter_by_globs(expanded, globs or [])

    files_scanned = len(expanded)
    matches: list[GrepMatchModel] = []
    per_file_counts: dict[pathlib.Path, int] = {}
    total_matches = 0
    for record in pyfltr.grep_.scanner.scan_files(
        expanded,
        compiled,
        before_context=before_ctx,
        after_context=after_ctx,
        max_per_file=max_count,
        max_total=effective_max_total,
        encoding=encoding,
        max_filesize=max_filesize,
        multiline=multiline,
    ):
        if isinstance(record, MatchRecord):
            total_matches += 1
            per_file_counts[record.file] = per_file_counts.get(record.file, 0) + 1
            if summary_mode is None:
                matches.append(
                    GrepMatchModel(
                        file=pyfltr.paths.normalize_separators(record.file),
                        line=record.line,
                        col=record.col,
                        end_col=record.end_col,
                        match_text=record.match_text,
                        line_text=record.line_text,
                        before=list(record.before_lines),
                        after=list(record.after_lines),
                    )
                )

    files_with_matches = (
        [pyfltr.paths.normalize_separators(file) for file in per_file_counts] if summary_mode == "files_with_matches" else []
    )
    file_counts = (
        [
            GrepFileCountModel(file=pyfltr.paths.normalize_separators(file), count=count)
            for file, count in per_file_counts.items()
        ]
        if summary_mode == "count"
        else []
    )
    files_without_match = (
        [pyfltr.paths.normalize_separators(file) for file in expanded if file not in per_file_counts]
        if summary_mode == "files_without_match"
        else []
    )
    return GrepResultModel(
        matches=matches,
        total_matches=total_matches,
        files_scanned=files_scanned,
        exit_code=0 if total_matches > 0 else 1,
        fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
        missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
        summary_mode=summary_mode,
        files_with_matches=files_with_matches,
        file_counts=file_counts,
        files_without_match=files_without_match,
    )


async def tool_replace(
    pattern: str,
    replacement: str,
    paths: list[str],
    dry_run: bool = True,
    ignore_case: bool = False,
    smart_case: bool = False,
    fixed_strings: bool = False,
    word_regexp: bool = False,
    line_regexp: bool = False,
    multiline: bool = False,
    within: str | None = None,
    before_context: int = 0,
    after_context: int = 0,
    context: int | None = None,
    types: list[str] | None = None,
    globs: list[str] | None = None,
    encoding: str = "utf-8",
    max_filesize: int | None = None,
    exclude_files: list[str] | None = None,
    from_grep: str | None = None,
    no_exclude: bool = False,
    no_gitignore: bool = False,
    show_changes: bool = False,
) -> ReplaceResultModel:
    r"""指定ファイル群へ正規表現置換を適用し、変更内容を返す。

    `dry_run=True`（既定）はファイルを変更せず変更内容のみを返す。
    `dry_run=False`を明示した場合のみ実書き込みし、`replace_id`を返す。
    `dry_run`の既定値がCLI（`False`）と異なるのはLLM暴発防止のため
    （`.claude/skills/grep-replace/SKILL.md`参照）。

    Args:
        pattern: 検索パターン（正規表現）。
        replacement: 置換式（`re.sub`互換、`\\1`/`\\g<name>`参照可）。
        paths: 対象のファイルまたはディレクトリパスの一覧。
        dry_run: Trueの場合（既定）、ファイルを変更せず変更内容のみ計算する。
        ignore_case: 大文字小文字を区別しない。
        smart_case: パターンに大文字を含まない場合のみignore_caseを有効化する。
        fixed_strings: パターンを固定文字列として扱う。
        word_regexp: 単語境界で囲まれたマッチのみ採用する。
        line_regexp: 行全体に一致したマッチのみ採用する。
        multiline: マルチラインマッチを有効化する。
        within: アンカー正規表現。指定時はアンカー行と前後コンテキスト
            （`before_context`/`after_context`）で定まる領域内のみ置換する。
        before_context: `within`領域でアンカー行の前に含める行数（CLIの`-B`相当、既定0でアンカー行のみ）。
            `within`なしで指定した場合はエラーとなる。
        after_context: `within`領域でアンカー行の後に含める行数（CLIの`-A`相当、既定0でアンカー行のみ）。
            `within`なしで指定した場合はエラーとなる。
        context: `within`領域でアンカー行の前後に含める行数の一括指定。
            `within`なしで指定した場合はエラーとなる。
        types: 対象言語タイプの一覧。
        globs: globパターンでの対象限定一覧。
        encoding: ファイル読み込み・書き込み時のエンコーディング（既定: utf-8）。
        max_filesize: 走査対象ファイルサイズの上限（バイト単位）。
        exclude_files: 置換対象から除外するファイルパスの一覧。
        from_grep: grepのJSONL出力パス。当該出力に現れるファイル集合へ対象を限定する。
        no_exclude: exclude/extend-excludeによる除外を無効化する。
        no_gitignore: .gitignoreによる除外を無効化する。
        show_changes: Trueの場合、`changes`フィールドに各置換箇所の変更前後を含める。
    """
    if not paths:
        _raise_mcp_error("paths を 1 件以上指定してください。")

    # CLIの`-A`/`-B`/`-C`拒否方針と対称に、`within`なしのコンテキスト指定を拒否する。
    if within is None and (before_context or after_context or context is not None):
        _raise_mcp_error("before_context / after_context / context は within と併用してください。")
    # `within`は行範囲で領域を定めるためマルチラインとは併用不可。
    if within is not None and multiline:
        _raise_mcp_error("within と multiline は併用できません。")

    before_ctx = before_context
    after_ctx = after_context
    if within is not None and context is not None:
        if after_ctx == 0:
            after_ctx = context
        if before_ctx == 0:
            before_ctx = context

    # warnings_はモジュールグローバルに蓄積するため、リクエスト開始時に初期化する
    pyfltr.warnings_.clear()
    try:
        compiled = pyfltr.grep_.matcher.compile_pattern(
            [pattern],
            fixed_strings=fixed_strings,
            ignore_case=ignore_case,
            smart_case=smart_case,
            word_regexp=word_regexp,
            line_regexp=line_regexp,
            multiline=multiline,
        )
        anchor = (
            pyfltr.grep_.matcher.compile_pattern(
                [within],
                fixed_strings=fixed_strings,
                ignore_case=ignore_case,
                smart_case=smart_case,
                word_regexp=word_regexp,
                line_regexp=line_regexp,
                multiline=False,
            )
            if within is not None
            else None
        )
    except ValueError as exc:
        _raise_mcp_error(str(exc))

    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        _raise_mcp_error(f"設定エラー: {exc}")

    if no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if no_gitignore:
        config.values["respect-gitignore"] = False

    expanded = pyfltr.command.targets.expand_all_files(
        [pathlib.Path(p) for p in paths],
        config,
    )
    expanded = pyfltr.grep_.scanner.filter_files_by_type(expanded, types or [])
    expanded = pyfltr.grep_.scanner.filter_by_globs(expanded, globs or [])

    # exclude_filesによる対象限定
    if exclude_files:
        excluded = {pathlib.Path(p).resolve() for p in exclude_files}
        expanded = [p for p in expanded if p.resolve() not in excluded]
    if from_grep is not None:
        try:
            allowed = pyfltr.cli.replace_subcmd.read_from_grep(pathlib.Path(from_grep))
        except ValueError as exc:
            _raise_mcp_error(str(exc))
        expanded = [path for path in expanded if path.resolve() in allowed]

    replace_id = pyfltr.grep_.history.generate_replace_id() if not dry_run else None
    history_entries: list[dict[str, typing.Any]] = []
    file_changes: list[ReplaceFileChangeModel] = []
    change_records: list[ReplaceChangeRecordModel] = []
    total_replacements = 0
    files_changed = 0

    for file in expanded:
        if max_filesize is not None and max_filesize > 0:
            try:
                if file.stat().st_size > max_filesize:
                    continue
            except OSError:
                continue
        try:
            if anchor is not None:
                before, after, count, records = pyfltr.grep_.replacer.apply_block_replace_to_file(
                    file,
                    compiled,
                    replacement,
                    anchor,
                    before_context=before_ctx,
                    after_context=after_ctx,
                    encoding=encoding,
                )
            else:
                before, after, count, records = pyfltr.grep_.replacer.apply_replace_to_file(
                    file,
                    compiled,
                    replacement,
                    encoding=encoding,
                )
        except (UnicodeDecodeError, OSError):
            continue
        if count == 0:
            continue

        files_changed += 1
        total_replacements += count
        before_hash = pyfltr.grep_.replacer.compute_hash(before)
        after_hash = pyfltr.grep_.replacer.compute_hash(after)

        file_changes.append(
            ReplaceFileChangeModel(
                file=pyfltr.paths.normalize_separators(file),
                count=count,
                before_hash=before_hash,
                after_hash=after_hash,
            )
        )

        if show_changes:
            for record in records:
                change_records.append(
                    ReplaceChangeRecordModel(
                        file=pyfltr.paths.normalize_separators(record.file),
                        line=record.line,
                        col=record.col,
                        before_line=record.before_line,
                        after_line=record.after_line,
                    )
                )

        if not dry_run:
            file.write_text(after, encoding=encoding)
            history_entries.append(
                {
                    "file": file,
                    "before_content": before,
                    "after_hash": after_hash,
                    "records": list(records),
                }
            )

    # 実書き込み時に履歴を保存する
    if not dry_run and history_entries and replace_id is not None:
        meta = ReplaceCommandMeta(
            replace_id=replace_id,
            dry_run=False,
            fixed_strings=fixed_strings,
            pattern=pattern,
            replacement=replacement,
            encoding=encoding,
        )
        store = pyfltr.grep_.history.ReplaceHistoryStore()
        store.save_replace(replace_id, command_meta=meta, file_changes=history_entries)
        store.cleanup(pyfltr.grep_.history.policy_from_config(config))

    return ReplaceResultModel(
        replace_id=replace_id,
        dry_run=dry_run,
        files_changed=files_changed,
        total_replacements=total_replacements,
        file_changes=file_changes,
        changes=change_records,
        exit_code=0,
        fully_excluded_files=pyfltr.warnings_.filtered_direct_files(reason="excluded"),
        missing_targets=pyfltr.warnings_.filtered_direct_files(reason="missing"),
    )


async def tool_replace_undo(replace_id: str, force: bool = False) -> ReplaceUndoModel:
    """保存済みreplace履歴からファイルを変更前の内容へ復元する。

    `force=True`を指定しない限り、手動編集済み（ハッシュ不一致）のファイルはスキップする。
    スキップが発生した場合は`exit_code=1`を返す。クライアント側で`force=True`再呼び出しの
    判断材料にする。

    Args:
        replace_id: undo対象のreplace識別子（ULID）。
        force: Trueの場合、ハッシュ不一致のファイルも強制復元する。
    """
    store = pyfltr.grep_.history.ReplaceHistoryStore()
    try:
        restored, skipped = store.undo_replace(replace_id, force=force)
    except FileNotFoundError:
        _raise_mcp_error(f"replace_id が見つかりません: {replace_id}")

    exit_code = 1 if skipped else 0
    return ReplaceUndoModel(
        replace_id=replace_id,
        restored=[pyfltr.paths.normalize_separators(p) for p in restored],
        skipped=[pyfltr.paths.normalize_separators(p) for p in skipped],
        exit_code=exit_code,
    )


async def tool_replace_history(
    action: str = "list",
    replace_id: str | None = None,
    limit: int | None = None,
) -> ReplaceHistoryModel:
    """replace履歴を一覧または単体で参照する。

    対応CLI: `pyfltr replace --list-history` / `pyfltr replace --show-history <ID>`

    Args:
        action: `"list"`（一覧、既定）または`"show"`（単体）。
        replace_id: `action="show"`時に必須のreplace識別子。
        limit: `action="list"`時の最大件数。省略時は全件。
    """
    pyfltr.warnings_.clear()
    if action not in ("list", "show"):
        _raise_mcp_error("action は list / show のいずれかを指定してください。")
    if action == "show" and replace_id is None:
        _raise_mcp_error('action="show"では replace_id を指定してください。')

    store = pyfltr.grep_.history.ReplaceHistoryStore()
    if action == "list":
        raw_entries = store.list_replaces(limit=limit)
    else:
        try:
            raw_entries = [store.load_replace(typing.cast(str, replace_id))]
        except FileNotFoundError:
            _raise_mcp_error(f"replace_id が見つかりません: {replace_id}")

    entries = [
        ReplaceHistoryEntryModel(
            replace_id=str(entry["replace_id"]),
            saved_at=entry.get("saved_at"),
            command=dict(entry.get("command", {})),
            files=[
                ReplaceHistoryFileModel(
                    file=str(file_entry["file"]),
                    records_count=int(file_entry.get("records_count", 0)),
                )
                for file_entry in entry.get("files", [])
            ],
        )
        for entry in raw_entries
    ]
    return ReplaceHistoryModel(action=action, entries=entries)


async def tool_command_info(command: str, check: bool = False) -> CommandInfoModel:
    """ツールの起動方式の解決結果を返す。

    対応CLI: `pyfltr command-info <command> [--check]`

    Args:
        command: 対象のツール名。
        check: Trueの場合、実行経路と同じ事前確認を行う。miseのtrust試行や
            パッケージマネージャーの版確認などの副作用が発生し得るため、既定はFalse。
    """
    pyfltr.warnings_.clear()
    try:
        config = pyfltr.config.config.load_config()
    except (ValueError, OSError) as exc:
        _raise_mcp_error(f"設定エラー: {exc}")
    try:
        pyfltr.cli.command_selection.validate_commands([command], config)
    except ValueError as exc:
        _raise_mcp_error(str(exc))
    info = pyfltr.cli.command_info.collect_info(command, config, do_check=check)
    return CommandInfoModel(command=command, resolved=bool(info.get("resolved", True)), info=info)


async def tool_config(
    action: str,
    key: str | None = None,
    value: str | None = None,
    use_global: bool = False,
    include_defaults: bool = False,
) -> ConfigResultModel:
    """pyfltr設定ファイルを操作する。

    対応CLI: `pyfltr config get|set|delete|list`

    Args:
        action: `"get"` / `"set"` / `"delete"` / `"list"`のいずれか。
        key: get、set、deleteで必須の設定キー名。
        value: setで必須の設定値。キーの型に応じて変換する。
        use_global: Trueの場合、グローバル設定ファイルを対象にする。
        include_defaults: listで既定値のままのキーも含める。
    """
    pyfltr.warnings_.clear()
    if action not in ("get", "set", "delete", "list"):
        _raise_mcp_error("action は get / set / delete / list のいずれかを指定してください。")
    if action in ("get", "delete"):
        if key is None:
            _raise_mcp_error(f'action="{action}"では key を指定してください。')
        if value is not None:
            _raise_mcp_error(f'action="{action}"では value を指定できません。')
    elif action == "set":
        if key is None or value is None:
            _raise_mcp_error('action="set"では key と value を指定してください。')
    elif key is not None or value is not None:
        _raise_mcp_error('action="list"では key と value を指定できません。')
    if include_defaults and action != "list":
        _raise_mcp_error('include_defaults は action="list"のときのみ指定できます。')

    path = pyfltr.config.config.default_global_config_path() if use_global else pathlib.Path("pyproject.toml").absolute()
    if action == "get":
        try:
            values = pyfltr.config.config.read_config_values(path)
        except (ValueError, OSError) as exc:
            _raise_mcp_error(str(exc))
        requested_key = typing.cast(str, key)
        result_value: typing.Any = None
        is_default = False
        if requested_key in values:
            result_value = values[requested_key]
        elif requested_key in pyfltr.config.config.DEFAULT_CONFIG:
            result_value = pyfltr.config.config.DEFAULT_CONFIG[requested_key]
            is_default = True
        else:
            _raise_mcp_error(
                pyfltr.config.config.format_unknown_key_message(
                    requested_key,
                    pyfltr.config.config.DEFAULT_CONFIG.keys(),
                )
            )
        return ConfigResultModel(
            action=action,
            path=str(path),
            key=requested_key,
            value=result_value,
            is_default=is_default,
        )

    if action == "set":
        requested_key = typing.cast(str, key)
        raw_value = typing.cast(str, value)
        if requested_key not in pyfltr.config.config.DEFAULT_CONFIG:
            _raise_mcp_error(
                pyfltr.config.config.format_unknown_key_message(
                    requested_key,
                    pyfltr.config.config.DEFAULT_CONFIG.keys(),
                )
            )
        try:
            parsed_value = pyfltr.config.config.parse_config_value(requested_key, raw_value)
        except ValueError as exc:
            _raise_mcp_error(str(exc))
        if requested_key in pyfltr.config.config.GLOBAL_PRIORITY_KEYS and not use_global:
            pyfltr.warnings_.emit_warning(
                source="config",
                message=(
                    f"{requested_key} はarchive/cache系のキーです。マシン共通設定として"
                    " --global での設定を推奨します（global側があればglobal優先になります）。"
                ),
            )
        elif requested_key not in pyfltr.config.config.GLOBAL_PRIORITY_KEYS and use_global:
            pyfltr.warnings_.emit_warning(
                source="config",
                message=(
                    f"{requested_key} は通常キーのためproject側のpyproject.tomlが優先されます。"
                    " globalに書いてもproject側に同じキーがあれば上書きされます。"
                ),
            )
        try:
            pyfltr.config.config.set_config_value(
                path,
                requested_key,
                parsed_value,
                create_if_missing=use_global,
            )
        except (ValueError, OSError) as exc:
            _raise_mcp_error(str(exc))
        return ConfigResultModel(
            action=action,
            path=str(path),
            key=requested_key,
            value=parsed_value,
            warnings=[str(entry["message"]) for entry in pyfltr.warnings_.collected_warnings()],
        )

    if action == "delete":
        requested_key = typing.cast(str, key)
        if requested_key not in pyfltr.config.config.DEFAULT_CONFIG:
            _raise_mcp_error(
                pyfltr.config.config.format_unknown_key_message(
                    requested_key,
                    pyfltr.config.config.DEFAULT_CONFIG.keys(),
                )
            )
        try:
            existed = pyfltr.config.config.delete_config_value(path, requested_key)
        except (ValueError, OSError) as exc:
            _raise_mcp_error(str(exc))
        return ConfigResultModel(action=action, path=str(path), key=requested_key, existed=existed)

    try:
        explicit_values = pyfltr.config.config.read_config_values(path)
    except (ValueError, OSError) as exc:
        _raise_mcp_error(str(exc))
    if include_defaults:
        listed_values: dict[str, typing.Any] = {
            item_key: {
                "value": explicit_values.get(item_key, default_value),
                "default": item_key not in explicit_values,
            }
            for item_key, default_value in sorted(pyfltr.config.config.DEFAULT_CONFIG.items())
        }
    else:
        listed_values = explicit_values
    return ConfigResultModel(action=action, path=str(path), values=listed_values)


# ---------------------------------------------------------------------------
# MCPServer組み立て
# ---------------------------------------------------------------------------


def build_server() -> MCPServer:
    """MCPServerインスタンスを生成し、11ツールを登録して返す。

    公開名は`@mcp.tool(name=...)`で明示し、Python側の関数名（`tool_*`）
    とは独立したスキーマ名（`list_runs`等）を維持する。
    """
    if _mcpserver is None:
        raise RuntimeError("MCPサーバー機能に必要な依存を読み込めません") from _MCP_IMPORT_ERROR
    mcp = _mcpserver.MCPServer("pyfltr")

    mcp.tool(name="list_runs", description="実行アーカイブに保存された run 一覧を新しい順で返す。")(tool_list_runs)
    mcp.tool(
        name="show_run", description="指定 run の meta 情報とコマンド別サマリを返す。run_id は前方一致・latest エイリアス可。"
    )(tool_show_run)
    mcp.tool(name="show_run_diagnostics", description="指定 run・コマンドの tool.json と diagnostics 全件を返す。")(
        tool_show_run_diagnostics
    )
    mcp.tool(name="show_run_output", description="指定 run・コマンドの output.log 全文を返す。")(tool_show_run_output)
    mcp.tool(
        name="run_for_agent",
        description=(
            "指定パスに対してlint/format/testを実行し、run_id・終了コード・失敗コマンド名を返す。"
            " modeでrun・fast・ciを選択し、CLIと同じ対象制御オプションを利用できる。"
            " only_failed=True で直前 run の失敗ツール・失敗ファイルのみ再実行する（from_run で参照 run を指定可）。"
            " 戻り値に retry_commands（失敗コマンドの再実行シェルコマンド）を含む。"
        ),
    )(tool_run_for_agent)
    mcp.tool(
        name="grep",
        description=(
            "Search for a regex pattern across files. Honors pyfltr exclude/.gitignore by default. Returns match records."
        ),
    )(tool_grep)
    mcp.tool(
        name="replace",
        description=(
            "Replace pattern with replacement across files."
            " dry_run=True (default) previews changes without writing."
            " Pass dry_run=False to write and save undo history."
        ),
    )(tool_replace)
    mcp.tool(
        name="replace_undo",
        description=(
            "Undo a previous replace by replace_id."
            " Set force=True to override hash mismatch (when files were edited after the replace)."
        ),
    )(tool_replace_undo)
    mcp.tool(
        name="replace_history",
        description="replace履歴を一覧（action=list）または単体（action=show）で返す。",
    )(tool_replace_history)
    mcp.tool(
        name="command_info",
        description=(
            "ツールの起動方式（runner・実行ファイル・最終コマンドライン等）の解決結果を返す。"
            " check=Trueはmiseの実行や版確認の副作用を伴う。"
        ),
    )(tool_command_info)
    mcp.tool(
        name="config",
        description="pyfltr設定ファイルを操作する（action=get / set / delete / list）。",
    )(tool_config)

    return mcp


# ---------------------------------------------------------------------------
# サブコマンド登録・エントリポイント
# ---------------------------------------------------------------------------


def register_subparsers(subparsers: typing.Any) -> None:
    """`mcp`サブパーサーを登録する。

    `subparsers`は`ArgumentParser.add_subparsers()`の戻り値
    （`argparse._SubParsersAction`）を想定する。
    """
    subparsers.add_parser(
        "mcp",
        help="MCP サーバーを stdio で起動する。",
    )


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
            "MCPサーバー機能に必要な依存が解決されていません。pyproject.tomlが宣言する版指定を満たす環境で実行してください: %s",
            _MCP_IMPORT_ERROR,
        )
        return 1

    try:
        server = build_server()
        server.run(transport="stdio")
        return 0
    except Exception as e:  # MCPサーバー起動失敗をエージェント側へ非ゼロ終了で通知するため全例外を捕捉する
        logger.error("MCP サーバーの起動に失敗した: %s", e)
        return 1
