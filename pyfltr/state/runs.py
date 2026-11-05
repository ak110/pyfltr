"""実行アーカイブの読み取りとrun識別子の解決。"""

from __future__ import annotations

import datetime as dt
import typing

import pyfltr.state.archive


def format_run_not_found(run_id: str, *, list_runs: str) -> str:
    """run_idのmeta情報が見つからない場合の案内文を返す。

    CLIとMCPで同じ文面を使い、run一覧を確認する操作名（`list_runs`）だけを呼び出し側が渡す。
    """
    return f"run_id が見つかりません: {run_id}。{list_runs} で有効な run_id を確認できます"


def format_tool_result_missing(run_id: str, tool: str, *, show_run: str) -> str:
    """指定runに指定ツールの結果が保存されていない場合の案内文を返す。

    CLIとMCPで同じ文面を使い、run内のツール一覧を確認する操作名（`show_run`）だけを呼び出し側が渡す。
    """
    return (
        f"run {run_id} にツール {tool!r} の結果が保存されていません。"
        f"{show_run} でrun {run_id}に保存されたツール一覧を確認できます"
    )


class RunIdError(Exception):
    """run_id解決に失敗した際の例外。"""


def resolve_run_id(
    store: pyfltr.state.archive.ArchiveStore, raw: str, *, list_runs: str = "`pyfltr list-runs`", run: str = "`pyfltr run`"
) -> str:
    """run_id指定を解決する。

    `latest`エイリアス → 完全一致 → 前方一致の順に試す。前方一致が複数
    該当した場合は曖昧と判定してエラーとする。

    完全一致のみ受け付ける案は不採用。ULID 26文字を毎回手入力させるUXが
    現実的でなく、CLIからの`show-run` / `--from-run`利用とMCP経路の
    どちらでも先頭数文字での参照ニーズが強いため、前方一致と`latest`
    エイリアスを許容する。曖昧時はエラーで明示することで、誤ったrunの
    閲覧・再実行を防ぐ。
    """
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
    """`tools/`配下から各ツールの要約（status / diagnostics / elapsed / slow_tests）を集める。

    `elapsed`は対象のツールの実行に要した秒数である。実行アーカイブはキャッシュヒットした
    ツールを書き込まないため（`pipeline`・`ui`が`not result.cached`で分岐する）、
    本関数の戻り値へキャッシュ由来の値が現れることはない。復元値と実測値の区別が要る
    JSONL経路では`cached_elapsed`へのキー名切替で表現する。

    `slow_tests`はテスターが報告した遅いテスト一覧で、`tool.json`の保存時点で正規化済みである。
    pytestの全件は`show-run --commands`が返す生出力から参照できる。

    `started_at`・`finished_at`はツール本体区間の開始・終了時刻で、保存されている場合だけ含める。
    """
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
        for key in ("started_at", "finished_at"):
            if tool_meta.get(key) is not None:
                summary[key] = tool_meta[key]
        slow_tests = tool_meta.get("slow_tests")
        if slow_tests:
            summary["slow_tests"] = slow_tests
        summaries.append(summary)
    return summaries


def summarize_timing(meta: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """runの経過時間と実行枠の待機区間を、未確定の値を`None`として要約する。

    - `measured`: 待機区間を計測した版のrunか（`meta.json`が`slot_waits`を持つか）
    - `run_elapsed`: `started_at`から`finished_at`までの秒数。完了記録の無いrun（実行中・強制終了）は`None`
    - `slot_waits`: 保存済みの待機区間。未計測は`None`
    - `slot_wait_total`: 完了した待機区間の秒数の合計。未計測か、未完了の区間がある場合は`None`
    - `unfinished_slot_waits`: 未完了の待機区間の数。未計測は`None`

    ツール本体の秒数は`collect_tool_summaries`の各要素が持つ。
    並行して動いたツールの秒数の合計はrun全体の経過と一致しないため、本関数は合算しない。
    """
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
