from __future__ import annotations

import pathlib

import pyfltr.command.core_
import pyfltr.output.logging_
import pyfltr.paths
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.runs
import pyfltr.warnings_
from pyfltr.command.only_failed import ToolTargets


def apply_filter(
    args: pyfltr.run_options.RunOptions,
    commands: list[str],
    all_files: list[pathlib.Path],
    *,
    start_cwd: pathlib.Path,
    from_run: str | None = None,
    extra_candidates: dict[str, list[pathlib.Path]] | None = None,
    only_failed_option: str = "--only-failed",
    from_run_option: str = "--from-run",
    list_runs: str = "`pyfltr list-runs`",
    run: str = "`pyfltr run`",
) -> tuple[list[str], dict[str, ToolTargets] | None, bool]:
    if not getattr(args, "only_failed", False):
        return commands, None, False

    all_targets_operation = (
        "--only-failed を外して" if only_failed_option == "--only-failed" else "only_failed=False に変更して"
    )
    archive_unreadable_hint = (
        "キャッシュディレクトリの読み取り権限を確認してください。"
        f"解消できない場合は {all_targets_operation}全体を実行してください。"
    )
    try:
        store = pyfltr.state.archive.ArchiveStore()
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"実行アーカイブを読み取れないため、再実行の対象を決められず実行をスキップしました: {e}",
            hint=archive_unreadable_hint,
        )
        return commands, None, True

    resolved_run_id: str | None = None
    if from_run is not None and from_run != "latest":
        try:
            resolved_run_id = pyfltr.state.runs.resolve_run_id(store, from_run, list_runs=list_runs, run=run)
        except pyfltr.state.runs.RunIdError as e:
            pyfltr.warnings_.emit_warning(
                source="only-failed",
                message=f"{from_run_option} {from_run!r} を解決できないため実行をスキップしました: {e}",
                hint=f"{list_runs}で参照を確認して{from_run_option}を指定し直すか、{all_targets_operation}実行してください。",
            )
            return commands, None, True

    last_run = _load_run_summary(store, resolved_run_id=resolved_run_id, start_cwd=start_cwd, hint=archive_unreadable_hint)
    if last_run is None:
        if resolved_run_id is not None:
            message = "参照可能な直前の run が無いため、再実行の対象が無く実行をスキップしました。"
            hint = ""
        else:
            message = (
                f"この作業ディレクトリ ({start_cwd}) で行われた参照可能な直前の run が無いため、"
                "再実行の対象が無く実行をスキップしました。別の作業ディレクトリで行われた run は参照しません。"
            )
            hint = (
                f"別の作業ディレクトリの run を参照する場合は、{list_runs}で run_id を確認して"
                f"{from_run_option}に指定してください。"
            )
        _emit_skip_warning(
            message,
            hint=(
                f"{all_targets_operation}一度実行してください。"
                f"失敗があれば、その後 {only_failed_option} で失敗分だけを再実行できます。{hint}"
            ),
        )
        return commands, None, True

    run_label = _describe_run(last_run)
    failed_tools = _collect_failed_tools(store, last_run.run_id, commands, hint=archive_unreadable_hint)
    if not failed_tools:
        _emit_skip_warning(
            f"直前の run ({run_label}) に失敗したツールが無いため、実行をスキップしました。",
            hint=f"再実行は不要です。全体を確認し直す場合は {all_targets_operation}実行してください。",
        )
        return commands, None, True

    targets: dict[str, ToolTargets] = {}
    for tool in failed_tools:
        candidates = [*all_files, *(extra_candidates or {}).get(tool, [])]
        tool_targets = _extract_failed_files_for_tool(store, last_run.run_id, tool, candidates, start_cwd=start_cwd)
        if tool_targets is not None:
            targets[tool] = tool_targets

    filtered_commands = _filter_commands_with_targets(commands, targets)
    if not filtered_commands:
        _emit_skip_warning(
            f"直前の run ({run_label}) の失敗ファイルはいずれも指定した対象に含まれないため、実行をスキップしました。",
            hint="失敗したファイルを対象に含めるか、対象の指定を外して実行してください。",
        )
        return commands, None, True

    pyfltr.output.logging_.text_logger.info(
        f"--only-failed: 直前 run ({run_label}) から {len(filtered_commands)} ツールを対象として再実行します。"
    )
    return filtered_commands, targets, False


def _load_run_summary(
    store: pyfltr.state.archive.ArchiveStore,
    *,
    resolved_run_id: str | None = None,
    start_cwd: pathlib.Path,
    hint: str,
) -> pyfltr.state.archive.RunSummary | None:
    try:
        all_runs = store.list_runs()
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"実行アーカイブを読み取れません: {e}",
            hint=hint,
        )
        return None
    if resolved_run_id is not None:
        return next((r for r in all_runs if r.run_id == resolved_run_id), None)
    return next(
        (
            r
            for r in all_runs
            if r.finished_at is not None and r.cwd is not None and pyfltr.paths.is_same_location(r.cwd, start_cwd)
        ),
        None,
    )


def _describe_run(summary: pyfltr.state.archive.RunSummary) -> str:
    if summary.cwd is None:
        return summary.run_id
    return f"{summary.run_id}、作業ディレクトリ: {summary.cwd}"


def _collect_failed_tools(
    store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
    commands: list[str],
    *,
    hint: str,
) -> list[str]:
    try:
        tool_names = store.list_tools(run_id)
    except OSError as e:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"直前 run のツール一覧を読み取れません: {e}",
            hint=hint,
        )
        return []

    commands_set = set(commands)
    failed: list[str] = []
    for tool in tool_names:
        if tool not in commands_set:
            continue
        try:
            meta = store.read_tool_meta(run_id, tool)
        except OSError:
            continue
        if pyfltr.command.core_.is_failed_status(meta.get("status")):
            failed.append(tool)
    return failed


def _extract_failed_files_for_tool(
    store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
    tool: str,
    all_files: list[pathlib.Path],
    *,
    start_cwd: pathlib.Path,
) -> ToolTargets | None:
    try:
        diagnostics = store.read_tool_diagnostics(run_id, tool)
    except OSError:
        diagnostics = []

    failed_files = {d["file"] for d in diagnostics if isinstance(d.get("file"), str)}
    if not failed_files:
        return ToolTargets.fallback_default()

    failed_keys = {pyfltr.paths.to_match_key(f, start_cwd=start_cwd) for f in failed_files}
    all_files_map = {pyfltr.paths.to_match_key(p, start_cwd=start_cwd): p for p in all_files}
    intersected = [path for key, path in all_files_map.items() if key in failed_keys]
    if not intersected:
        pyfltr.warnings_.emit_warning(
            source="only-failed",
            message=f"{tool}: 直前 run の失敗ファイル {len(failed_files)} 件は指定 targets と交差しません。"
            "本ツールは対象から除外します。",
            hint="残りのツールの実行は継続するため対応不要です。このツールも再実行する場合は失敗したファイルを対象へ含めてください。",
        )
        return None
    return ToolTargets.with_files(intersected)


def _filter_commands_with_targets(
    commands: list[str],
    targets: dict[str, ToolTargets],
) -> list[str]:
    return [cmd for cmd in commands if cmd in targets]


def _emit_skip_warning(message: str, *, hint: str) -> None:
    pyfltr.warnings_.emit_warning(source="only-failed", message=message, hint=hint)
