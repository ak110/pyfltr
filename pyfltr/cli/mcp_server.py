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

# 本モジュールは`types`をgrep系ツール関数の引数名に使うため、標準ライブラリ側を別名で取り込む。
# 同名のままではモジュール名を引数が覆い、pylintの`redefined-outer-name`に抵触する。
import types as types_module
import typing

# 配布物の版指定は下流プロジェクトの依存解決で上書きされる場合がある。
# MCP専用依存のimport失敗を捕捉し、他サブコマンドとヘルプの起動を維持する。
try:
    import mcp.server.mcpserver as _imported_mcpserver
    import mcp.server.mcpserver.exceptions as _imported_mcp_exceptions

    import pyfltr.cli.mcp_transport as _imported_mcp_transport
except ImportError as e:  # 依存解決が配布物の宣言と異なる環境で到達する。
    _mcpserver: types_module.ModuleType | None = None
    _mcp_exceptions: types_module.ModuleType | None = None
    _mcp_transport: types_module.ModuleType | None = None
    _MCP_IMPORT_ERROR: ImportError | None = e
else:
    _mcpserver = _imported_mcpserver
    _mcp_exceptions = _imported_mcp_exceptions
    _mcp_transport = _imported_mcp_transport
    _MCP_IMPORT_ERROR = None

import pyfltr.cli.command_info
import pyfltr.cli.command_selection
import pyfltr.cli.overrides
import pyfltr.cli.pipeline
import pyfltr.cli.replace_subcmd
import pyfltr.command.core_
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.editing
import pyfltr.config.model
import pyfltr.config.operations
import pyfltr.config.selection
import pyfltr.config.validation
import pyfltr.grep_.adaptive
import pyfltr.grep_.history
import pyfltr.grep_.jsonl_records
import pyfltr.grep_.matcher
import pyfltr.grep_.operations
import pyfltr.grep_.preview
import pyfltr.grep_.replacer
import pyfltr.grep_.scanner
import pyfltr.output.jsonl
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.runs
import pyfltr.warnings_
from pyfltr.cli.mcp_models import (
    CommandDiagnosticsModel,
    CommandInfoModel,
    CommandMetaModel,
    CommandSummaryModel,
    ConfigResultModel,
    DiagnosticMessageModel,
    DiagnosticModel,
    GrepFileCountModel,
    GrepFileResultModel,
    GrepMatchModel,
    GrepResultModel,
    ReplaceChangeRecordModel,
    ReplaceFileChangeModel,
    ReplaceHistoryEntryModel,
    ReplaceHistoryFileModel,
    ReplaceHistoryModel,
    ReplaceResultModel,
    ReplaceUndoModel,
    RunOverviewModel,
    RunResult,
    RunSummaryModel,
    RunWarningModel,
    SlowTestModel,
)

if typing.TYPE_CHECKING:
    from mcp.server.mcpserver import MCPServer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# エラー変換ヘルパー
# ---------------------------------------------------------------------------


def _raise_mcp_error(msg: str) -> typing.Never:
    """MCPクライアントへエラーとして返すための例外を送出する。

    想定内の失敗はmcp SDKの`ToolError`で送出する。
    MCPServerは`ToolError`だけを本文付きの`is_error`応答へ変換し、
    それ以外の例外（`ValueError`など）はクラッシュとして扱い、
    本文を`Error executing tool <ツール名>`へ置き換える（mcp 2.1.0以降）。
    """
    if _mcp_exceptions is None:
        # ツール関数はbuild_server経由で呼ばれ、mcpを読み込めない環境ではbuild_serverが先に停止する。
        raise RuntimeError("MCPサーバー機能に必要な依存を読み込めません") from _MCP_IMPORT_ERROR
    raise _mcp_exceptions.ToolError(msg)


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
        _raise_mcp_error(pyfltr.state.runs.format_run_not_found(resolved, list_runs="`list_runs`ツール"))
    command_summaries = pyfltr.state.runs.collect_tool_summaries(store, resolved)
    commands = [
        CommandSummaryModel(
            command=entry.get("command"),
            status=entry.get("status"),
            diagnostics=entry.get("diagnostics"),
            elapsed=entry.get("elapsed"),
            slow_tests=[SlowTestModel.model_validate(test) for test in entry.get("slow_tests", [])],
        )
        for entry in command_summaries
    ]
    return RunOverviewModel(run_id=resolved, meta=meta, commands=commands)


async def tool_show_run_diagnostics(run_id: str, commands: list[str]) -> list[CommandDiagnosticsModel]:
    """指定run・コマンドのdiagnostics全件とコマンドのmeta情報を返す。

    `diagnostics`は`(command, file)`単位の集約形式で、個別指摘は`messages`に並ぶ。
    rule→URL辞書`hint_urls`とrule→ヒント辞書`hints`はtool.json由来でそのまま返す。
    `command_meta`はtool.jsonから`commandline`を除いた項目で、検査対象ファイルの引数列を含めない。
    完全な引数列は`pyfltr show-run <run_id> --commands=<name>`で取得する。
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
            tool_meta = store.read_tool_meta(resolved, command)
            diagnostics_raw = store.read_tool_diagnostics(resolved, command)
        except FileNotFoundError:
            _raise_mcp_error(pyfltr.state.runs.format_tool_result_missing(resolved, command, show_run="`show_run`ツール"))
        diagnostics = [
            DiagnosticModel(
                command=d.get("command", d.get("tool")),
                file=d.get("file"),
                messages=[DiagnosticMessageModel(**m) for m in d.get("messages", [])],
            )
            for d in diagnostics_raw
        ]
        hint_urls = tool_meta.get("hint_urls") if isinstance(tool_meta.get("hint_urls"), dict) else None
        hints = tool_meta.get("hints") if isinstance(tool_meta.get("hints"), dict) else None
        results.append(
            CommandDiagnosticsModel(
                command_meta=CommandMetaModel.model_validate(tool_meta),
                diagnostics=diagnostics,
                hint_urls=hint_urls,
                hints=hints,
            )
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
            _raise_mcp_error(pyfltr.state.runs.format_tool_result_missing(resolved, command, show_run="`show_run`ツール"))
    return outputs


async def tool_run(
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
) -> RunResult:
    """指定パスに対してlint/format/testを実行し、結果を返す。

    `run`・`fast`・`ci`の各実行モードをCLIと同じ既定値で扱う。
    実行アーカイブは常に有効化され、`run_id`を戻り値に含む。
    `only_failed`による早期終了（直前runなし・失敗ツールなし・対象ファイル交差が空・
    `from_run`の解決失敗・アーカイブの読み取り失敗）の場合は`run_id=None`とし、
    実際の理由と対処を`skipped_reason`へ設定して返す。
    `commands`へ指定した検査が設定で無効化されているために実行されなかった場合も、
    対象の検査名と理由を`skipped_reason`へ設定する。いずれも`run_pipeline`が発行する
    `source="only-failed"`・`source="commands"`の警告を入力とし、MCP側では再判定しない。
    実行中の警告は`warnings`へ、失敗時などの次の操作は`guidance`へ設定する。
    返却へ含めた警告はサーバーのstderrへ重ねて出力しない。

    結果は`completion`を最初に読む。`completed`以外なら`incomplete_commands`・
    `missing_targets`・`fully_excluded_files`・`skipped_reason`・`warnings`で未完了の理由を、
    `failed`が空でなければ`show_run_diagnostics`で診断を確認する。
    完了判定は`run_pipeline`が導出した値をそのまま返し、CLI JSONLの`summary`と同じ値になる。

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
            抑止する対象はfixステージだけで、通常ステージのformatterは対象ファイルを書き換える。
            `ruff-format-by-check`が既定で有効なため、本引数の指定時も`ruff-format`による整形は行われる。
            書き換えを避ける場合は`commands`で対象をlinterへ限定する。
        fail_fast: Trueの場合、1ツールでもエラーが発生した時点で残りを打ち切る。
        only_failed: Trueの場合、直前runの失敗ツールを失敗ファイルに限定して再実行する。
            診断にファイルを持たない失敗ツールは現在の対象全体で再実行する。
        from_run: `only_failed=True`時の参照run_id（前方一致・`latest`可）。
            `only_failed=False`かつ`from_run`指定はツールエラー（`ToolError`）。
        changed_since: 指定したgit参照から変更されたファイルだけを対象にする。
        work_dir: 実行の起点ディレクトリ。設定探索と相対パス解決の基準を兼ねる。
            省略時はMCPサーバープロセスのカレントディレクトリを用いる。
            指定時は診断のファイルパスを絶対パスで返す場合がある。
        allow_external_paths: Trueの場合、実行起点の外側にあるパスを許可する。
            `work_dir`には検査設定を持つプロジェクトのルートを指定する。
            `work_dir`は設定探索と相対パス解決の基準を兼ねるため、検査設定を持たないディレクトリを
            起点にすると、適用される除外設定と検査対象の範囲が変わる。
        no_exclude: Trueの場合、設定の除外パターンを無効化する。
        no_gitignore: Trueの場合、`.gitignore`による除外を無効化する。
        no_cache: Trueの場合、ファイルhashキャッシュを無効化する。
        human_readable: Trueの場合、対応ツールの機械可読出力を抑止する。
        shuffle: Trueの場合、実行対象ファイルの順序をシャッフルする。
        exit_zero_even_if_formatted: Trueの場合、formatterによる変更だけなら成功扱いにする。
        jobs: 並列実行するツール数の上限。
    """
    # `run_pipeline`は警告を初期化しないため、対象の呼び出しが発行した警告だけを
    # `skipped_reason`の入力にできるよう、ここで蓄積を初期化する。
    pyfltr.warnings_.clear()

    # 返却へ含めた警告を配送済みとして扱い、サーバーのstderrへ重ねて出力しないよう、
    # 返却の組み立てまでを配送保留スコープに含める。例外で返却に至らなかった警告は
    # スコープの終了時にstderrへ通知される。
    with pyfltr.warnings_.defer_stderr():
        if mode not in ("run", "fast", "ci"):
            _raise_mcp_error("mode は run / fast / ci のいずれかを指定してください。")
        if from_run is not None and not only_failed:
            _raise_mcp_error("from_run は only_failed=True のときのみ指定できます。")

        work_dir_path = pathlib.Path(work_dir).expanduser().resolve() if work_dir is not None else None
        if work_dir_path is not None and not work_dir_path.is_dir():
            _raise_mcp_error(f"work_dir が存在するディレクトリではありません: {work_dir}")

        base = work_dir_path if work_dir_path is not None else pathlib.Path.cwd()
        targets = [path if (path := pathlib.Path(raw)).is_absolute() else base / path for raw in paths]

        args = pyfltr.run_options.RunOptions(
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

        config = pyfltr.config.config.load_config(config_dir=work_dir_path)
        # アーカイブを強制有効化する。MCPツールはrun_idを返す契約を保証する。
        config.values["archive"] = True
        pyfltr.cli.overrides.apply_cli_overrides(config, args)

        commands_list: list[str] = pyfltr.config.selection.resolve_aliases(
            pyfltr.cli.command_selection.flatten_commands_arg(args.commands, config), config
        )
        try:
            pyfltr.cli.command_selection.validate_commands(
                commands_list, config, list_commands=pyfltr.cli.command_selection.MCP_LIST_COMMANDS
            )
        except ValueError as exc:
            _raise_mcp_error(str(exc))

        outcome = pyfltr.cli.pipeline.run_pipeline(
            args,
            commands_list,
            config,
            start_cwd=base,
            original_cwd=str(work_dir_path) if work_dir_path is not None else None,
            original_sys_args=args.retry_arguments(for_mcp=True),
            force_text_on_stderr=True,
            jsonl_warnings_reach_consumer=False,
        )

        exit_code = outcome.exit_code
        run_id = outcome.run_id
        completion = outcome.completion
        completion_fields: dict[str, typing.Any] = {
            "completion": completion.completion,
            "files_reached": completion.files_reached,
            "completed_commands": list(completion.completed_commands),
            "incomplete_commands": list(completion.incomplete_commands),
            "missing_targets": list(completion.missing_targets),
            "fully_excluded_files": list(completion.fully_excluded_files),
        }

        # run_idがNoneのときは実行がスキップされたか、アーカイブを使えなかった。
        # スキップの理由は`--only-failed`の判定が`source="only-failed"`の警告として発行しており、
        # 固定文ではなく実際の理由を返す。
        if run_id is None:
            warning_entries = pyfltr.warnings_.collected_warnings()
            pyfltr.warnings_.mark_delivered(warning_entries)
            return RunResult(
                run_id=None,
                exit_code=exit_code,
                **completion_fields,
                failed=[],
                commands=[],
                skipped_reason=_join_warning_texts(warning_entries, source="only-failed"),
                retry_commands={},
                warnings=_build_run_warnings(warning_entries),
            )

        commands_model = [
            CommandSummaryModel(
                command=result.command,
                status=result.status,
                diagnostics=len(result.errors),
                elapsed=result.elapsed,
                slow_tests=[SlowTestModel.model_validate(test.to_dict()) for test in result.slow_tests],
            )
            for result in outcome.results
        ]
        failed_commands = [result.command for result in outcome.results if result.failed]
        retry_commands = {result.command: result.retry_command for result in outcome.results if result.retry_command}

        warning_entries = pyfltr.warnings_.collected_warnings()
        pyfltr.warnings_.mark_delivered(warning_entries)
        guidance = pyfltr.output.jsonl.build_summary_guidance(
            failure_present=bool(failed_commands),
            resolution_failed_present=any(c.status == "resolution_failed" for c in commands_model),
            applied_fixes_present=any(c.status == "formatted" for c in commands_model),
            run_id=run_id,
            launcher_prefix=None,
            subcommand=mode,
        )
        return RunResult(
            run_id=run_id,
            exit_code=exit_code,
            **completion_fields,
            failed=failed_commands,
            commands=commands_model,
            skipped_reason=_join_warning_texts(warning_entries, source="commands"),
            retry_commands=retry_commands,
            warnings=_build_run_warnings(warning_entries),
            guidance=guidance,
        )


def _build_run_warnings(entries: list[dict[str, typing.Any]]) -> list[RunWarningModel]:
    """蓄積された警告を`RunResult.warnings`の要素へ変換する。"""
    return [
        RunWarningModel(source=str(entry["source"]), message=str(entry["message"]), hint=entry.get("hint")) for entry in entries
    ]


def _join_warning_texts(entries: list[dict[str, typing.Any]], *, source: str) -> str | None:
    """指定した発生源の警告を対処込みの文字列へ整形して連結する。該当が無ければNoneを返す。"""
    texts = [pyfltr.warnings_.format_warning_text(entry) for entry in entries if entry.get("source") == source]
    return " / ".join(texts) or None


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
    max_count: int | None = None,
    max_total: int | None = None,
    summary_mode: typing.Literal["files_with_matches", "count", "files_without_match"] | None = None,
    types: list[str] | None = None,
    globs: list[str] | None = None,
    encoding: str = "utf-8",
    max_filesize: int | None = None,
    max_preview_chars: int | None = None,
    auto_summary: bool = True,
    no_exclude: bool = False,
    no_gitignore: bool = False,
) -> GrepResultModel:
    """指定ファイル群から正規表現パターンを検索し、マッチ一覧を返す。

    pyfltrの`exclude`/`extend-exclude`/`respect-gitignore`設定を尊重する。
    通常検索でも未指定の`max_total`は走査を制限せず、全結果から返却形式を選択する。

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
        max_count: ファイル単位の最大マッチ件数。未指定または0で無制限。
        max_total: 全体の最大マッチ件数。未指定または0で無制限。
        summary_mode: 集計モード。`files_with_matches`、`count`、`files_without_match`のいずれか。
            指定時は`matches`を空で返し、対応する集計フィールドを返す。
            `files_without_match`では正の`max_total`を併用できない。
        types: 対象言語タイプの一覧（例: ["python", "ts"]）。
        globs: globパターンでの対象限定一覧。
        encoding: ファイル読み込み時のエンコーディング（既定: utf-8）。
        max_filesize: 走査対象ファイルサイズの上限（バイト単位）。
        max_preview_chars: 返却する本文1件あたりの文字数上限。未指定時は既定値、0で無制限。
        auto_summary: Trueの場合、明示上限がない大きな結果を適応的に縮約する。
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
            _raise_mcp_error(
                f"パターンファイルの読み込みに失敗しました: {exc}。`pattern_file`のパスと読み取り権限を確認してください"
            )
    if not collected:
        _raise_mcp_error(
            "パターンが指定されていません。`pattern`・`patterns`・`pattern_file`のいずれかで検索パターンを指定してください"
        )
    valid_summary_modes = ("files_with_matches", "count", "files_without_match")
    if summary_mode is not None and summary_mode not in valid_summary_modes:
        _raise_mcp_error("summary_mode は files_with_matches / count / files_without_match のいずれかを指定してください。")
    if summary_mode == "files_without_match" and max_total is not None and max_total > 0:
        _raise_mcp_error("summary_mode=files_without_match では max_total に正の値を指定できません。")
    request = pyfltr.grep_.operations.GrepRequest(
        patterns=collected,
        summary_only=summary_mode is not None,
        max_count=max_count,
        max_total=max_total,
        max_preview_chars=max_preview_chars,
        auto_summary=auto_summary,
        output_format="mcp",
        full_text_hint="`max_preview_chars=0`",
        targets=pyfltr.grep_.operations.TargetRequest.from_paths(paths, types or [], globs or [], no_exclude, no_gitignore),
        **pyfltr.grep_.operations.PatternOptions.arguments(
            (fixed_strings, ignore_case, smart_case, word_regexp, line_regexp, multiline),
            (before_context, after_context, context),
            encoding,
            max_filesize,
        ),
    )
    try:
        operation = pyfltr.grep_.operations.execute_grep(request)
    except ValueError as exc:
        _raise_mcp_error(str(exc))
    expanded = operation.expanded
    per_file_counts = operation.per_file_counts
    total_matches = operation.total_matches
    selection = operation.selection
    files_scanned = len(expanded)
    matches = [GrepMatchModel.model_validate(payload) for payload in selection.matches]
    adaptive_file_results = [GrepFileResultModel.model_validate(result) for result in selection.file_results]

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
        file_results=adaptive_file_results,
        output_mode=selection.output_mode,
        total_matches=total_matches,
        files_scanned=files_scanned,
        exit_code=0 if total_matches > 0 else 1,
        returned_matches=selection.returned_matches,
        omitted_matches=selection.omitted_matches,
        omitted_files=selection.omitted_files,
        guidance=(
            [
                "Narrow the search by file path, globs, or types to inspect omitted matches.",
                "Set auto_summary=false to return every match in the selected search range.",
            ]
            if selection.output_mode != "full"
            else []
        ),
        warnings=[pyfltr.warnings_.format_warning_text(entry) for entry in pyfltr.warnings_.collected_warnings()],
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
        from_grep: grepのJSONL出力パス。対象の出力に現れるファイル集合へ対象を限定する。
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

    pyfltr.warnings_.clear()
    request = pyfltr.grep_.operations.ReplaceRequest(
        pattern=pattern,
        replacement=replacement,
        dry_run=dry_run,
        within=within,
        exclude_files=[pathlib.Path(path) for path in exclude_files or []],
        from_grep=pathlib.Path(from_grep) if from_grep is not None else None,
        targets=pyfltr.grep_.operations.TargetRequest.from_paths(paths, types or [], globs or [], no_exclude, no_gitignore),
        **pyfltr.grep_.operations.PatternOptions.arguments(
            (fixed_strings, ignore_case, smart_case, word_regexp, line_regexp, multiline),
            (before_context, after_context, context),
            encoding,
            max_filesize,
        ),
    )
    try:
        operation = pyfltr.grep_.operations.execute_replace(request)
    except pyfltr.grep_.history.ReplaceFailure as exc:
        _raise_mcp_error(exc.describe(undo=f"`replace_undo(replace_id={exc.replace_id!r}, force=True)`"))
    except ValueError as exc:
        _raise_mcp_error(str(exc))
    replace_id = operation.replace_id
    file_changes = [
        ReplaceFileChangeModel(
            file=pyfltr.paths.normalize_separators(file),
            count=result.count,
            before_hash=pyfltr.grep_.replacer.compute_hash(result.before_content),
            after_hash=pyfltr.grep_.replacer.compute_hash(result.after_content),
        )
        for file, result in operation.prepared
    ]
    change_records = (
        [
            ReplaceChangeRecordModel(
                file=pyfltr.paths.normalize_separators(record.file),
                line=record.line,
                col=record.col,
                before_line=record.before_line,
                after_line=record.after_line,
            )
            for _file, result in operation.prepared
            for record in result.records
        ]
        if show_changes
        else []
    )
    files_changed = len(operation.prepared)
    total_replacements = sum(result.count for _file, result in operation.prepared)
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
        warnings=[pyfltr.warnings_.format_warning_text(entry) for entry in pyfltr.warnings_.collected_warnings()],
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
        restored, skipped, warnings = store.undo_replace(replace_id, force=force)
    except pyfltr.grep_.history.ReplaceFailure as exc:
        _raise_mcp_error(exc.describe(undo=f"`replace_undo(replace_id={replace_id!r}, force=True)`"))
    except FileNotFoundError:
        _raise_mcp_error(pyfltr.grep_.history.format_replace_id_not_found(replace_id, list_history="`replace_history`ツール"))
    except (UnicodeDecodeError, OSError) as exc:
        _raise_mcp_error(pyfltr.grep_.history.format_history_unreadable(replace_id, store.history_root / replace_id, exc))

    exit_code = 1 if skipped else 0
    if skipped:
        warnings = [*warnings, pyfltr.grep_.history.format_undo_skipped(len(skipped), force="`force=True`")]
    return ReplaceUndoModel(
        replace_id=replace_id,
        restored=[pyfltr.paths.normalize_separators(p) for p in restored],
        skipped=[pyfltr.paths.normalize_separators(p) for p in skipped],
        exit_code=exit_code,
        warnings=warnings,
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
            _raise_mcp_error(
                pyfltr.grep_.history.format_replace_id_not_found(
                    typing.cast(str, replace_id), list_history='`replace_history`ツールの`action="list"`'
                )
            )

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
        pyfltr.cli.command_selection.validate_commands(
            [command], config, list_commands=pyfltr.cli.command_selection.MCP_LIST_COMMANDS
        )
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
        )
    )
    if result.error is not None:
        _raise_mcp_error(result.error)
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

TOOL_HANDLERS = {
    handler.__name__: handler
    for handler in (
        tool_list_runs,
        tool_show_run,
        tool_show_run_diagnostics,
        tool_show_run_output,
        tool_run,
        tool_grep,
        tool_replace,
        tool_replace_undo,
        tool_replace_history,
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
    assert _mcp_transport is not None
    mcp = _mcpserver.MCPServer("pyfltr", version=importlib.metadata.version("pyfltr"))

    mcp.tool(name="list_runs", description="実行アーカイブに保存された run 一覧を新しい順で返す。")(
        _mcp_transport.isolate_tool(tool_list_runs, _raise_mcp_error)
    )
    mcp.tool(
        name="show_run", description="指定 run の meta 情報とコマンド別サマリを返す。run_id は前方一致・latest エイリアス可。"
    )(_mcp_transport.isolate_tool(tool_show_run, _raise_mcp_error))
    mcp.tool(
        name="show_run_diagnostics",
        description=(
            "指定run・コマンドのdiagnostics全件とコマンドのmeta情報を返す。meta情報は検査対象ファイルの引数列を含まない。"
        ),
    )(_mcp_transport.isolate_tool(tool_show_run_diagnostics, _raise_mcp_error))
    mcp.tool(name="show_run_output", description="指定 run・コマンドの output.log 全文を返す。")(
        _mcp_transport.isolate_tool(tool_show_run_output, _raise_mcp_error)
    )
    mcp.tool(
        name="run",
        description=(
            "指定パスに対してlint/format/testを実行し、run_id・終了コード・失敗コマンド名を返す。"
            " modeでrun・fast・ciを選択し、CLIと同じ対象制御オプションを利用できる。"
            " only_failed=True で直前 run の失敗ツールを失敗ファイルに限定して再実行する"
            "（診断にファイルを持たない失敗ツールは現在の対象全体で再実行する。from_run で参照 run を指定可）。"
            " 戻り値に retry_commands（失敗コマンドの再実行シェルコマンド）を含む。"
            " no_fix=True が抑止するのは fix ステージだけで、通常ステージの formatter は対象ファイルを書き換える"
            "（ruff-format-by-check が既定で有効なため ruff-format による整形は行われる）。"
            " 書き換えを避ける場合は commands で対象を linter へ限定する。"
            " allow_external_paths=True で起点外の絶対パスを検査する場合は、work_dir へ検査設定を持つ"
            "プロジェクトのルートを指定する。work_dir は設定探索と相対パス解決の基準を兼ねるため、"
            "検査設定を持たないディレクトリを起点にすると適用される除外設定と検査対象の範囲が変わる。"
        ),
    )(_mcp_transport.isolate_tool(tool_run, _raise_mcp_error))
    mcp.tool(
        name="grep",
        description=(
            "Search for a regex pattern across files. Honors pyfltr exclude/.gitignore by default. Returns match records."
        ),
    )(_mcp_transport.isolate_tool(tool_grep, _raise_mcp_error))
    mcp.tool(
        name="replace",
        description=(
            "Replace pattern with replacement across files."
            " dry_run=True (default) previews changes without writing."
            " Pass dry_run=False to write and save undo history."
        ),
    )(_mcp_transport.isolate_tool(tool_replace, _raise_mcp_error))
    mcp.tool(
        name="replace_undo",
        description=(
            "Undo a previous replace by replace_id."
            " Set force=True to override hash mismatch (when files were edited after the replace)."
        ),
    )(_mcp_transport.isolate_tool(tool_replace_undo, _raise_mcp_error))
    mcp.tool(
        name="replace_history",
        description="replace履歴を一覧（action=list）または単体（action=show）で返す。",
    )(_mcp_transport.isolate_tool(tool_replace_history, _raise_mcp_error))
    mcp.tool(
        name="command_info",
        description=(
            "ツールの起動方式（runner・実行ファイル・最終コマンドライン等）の解決結果を返す。"
            " check=Trueはmiseの実行や版確認の副作用を伴う。"
        ),
    )(_mcp_transport.isolate_tool(tool_command_info, _raise_mcp_error))
    mcp.tool(
        name="config",
        description="pyfltr設定ファイルを操作する（action=get / set / delete / list）。",
    )(_mcp_transport.isolate_tool(tool_config, _raise_mcp_error))

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
        logger.error(
            "MCP サーバーの起動に失敗しました: %s。"
            "https://ak110.github.io/pyfltr/guide/troubleshooting/ のMCP関連の節で切り分け手順を確認してください",
            e,
        )
        return 1
