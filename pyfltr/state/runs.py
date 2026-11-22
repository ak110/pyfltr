from __future__ import annotations

import datetime as dt
import typing

import pyfltr.state.archive


def format_run_not_found(run_id: str, *, list_runs: str) -> str:
    return f"run_id が見つかりません: {run_id}。{list_runs} で有効な run_id を確認できます"


def format_tool_result_missing(run_id: str, tool: str, *, show_run: str) -> str:
    return (
        f"run {run_id} にツール {tool!r} の結果が保存されていません。"
        f"{show_run} でrun {run_id}に保存されたツール一覧を確認できます"
    )


class RunIdError(Exception):
    pass


def resolve_run_id(
    store: pyfltr.state.archive.ArchiveStore, raw: str, *, list_runs: str = "`pyfltr list-runs`", run: str = "`pyfltr run`"
) -> str:
    run_ids = [s.run_id for s in store.list_runs()]
    if raw == "latest":
        if not run_ids:
            raise RunIdError(f"アーカイブに run が存在しません。{run} で実行アーカイブを生成してください")
        return run_ids[0]
    if raw in run_ids:
        return raw
    matched = [rid for rid in run_ids if rid.startswith(raw)]
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        sample = ", ".join(matched[:3])
        suffix = "..." if len(matched) > 3 else ""
        raise RunIdError(
            f"run_id のプレフィックスが曖昧です: {raw!r} に {len(matched)} 件該当 ({sample}{suffix})。"
            "プレフィックスを長くして特定してください"
        )
    raise RunIdError(f"run_id が見つかりません: {raw!r}。{list_runs} で有効な run_id を確認できます")


def collect_tool_summaries(
    store: pyfltr.state.archive.ArchiveStore,
    run_id: str,
) -> list[dict[str, typing.Any]]:
    summaries: list[dict[str, typing.Any]] = []
    for tool in store.list_tools(run_id):
        try:
            tool_meta = store.read_tool_meta(run_id, tool)
        except FileNotFoundError:
            continue
        summary: dict[str, typing.Any] = {
            "command": tool_meta.get("command", tool_meta.get("tool", tool)),
            "status": tool_meta.get("status"),
            "diagnostics": tool_meta.get("diagnostics"),
            "elapsed": tool_meta.get("elapsed"),
        }
        for key in ("started_at", "finished_at", "launch_boundary_at"):
            if tool_meta.get(key) is not None:
                summary[key] = tool_meta[key]
        slow_tests = tool_meta.get("slow_tests")
        if slow_tests:
            summary["slow_tests"] = slow_tests
        summaries.append(summary)
    return summaries


def summarize_timing(meta: dict[str, typing.Any]) -> dict[str, typing.Any]:
    run_elapsed = _elapsed_between(meta.get("started_at"), meta.get("finished_at"))
    raw_waits = meta.get(pyfltr.state.archive.SLOT_WAITS_META_KEY)
    if not isinstance(raw_waits, list):
        return {
            "measured": False,
            "run_elapsed": run_elapsed,
            "slot_waits": None,
            "slot_wait_total": None,
            "unfinished_slot_waits": None,
        }
    waits = [wait for wait in raw_waits if isinstance(wait, dict)]
    unfinished = sum(1 for wait in waits if not isinstance(wait.get("elapsed"), int | float))
    total = None if unfinished else round(sum(float(wait["elapsed"]) for wait in waits), 3)
    return {
        "measured": True,
        "run_elapsed": run_elapsed,
        "slot_waits": waits,
        "slot_wait_total": total,
        "unfinished_slot_waits": unfinished,
    }


def _elapsed_between(started_at: typing.Any, finished_at: typing.Any) -> float | None:
    if not isinstance(started_at, str) or not isinstance(finished_at, str):
        return None
    try:
        delta = dt.datetime.fromisoformat(finished_at) - dt.datetime.fromisoformat(started_at)
    except ValueError:
        return None
    return round(delta.total_seconds(), 3)
