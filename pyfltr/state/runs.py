"""実行アーカイブの読み取りとrun識別子の解決。"""

from __future__ import annotations

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


def resolve_run_id(store: pyfltr.state.archive.ArchiveStore, raw: str) -> str:
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
            raise RunIdError("アーカイブに run が存在しません。`pyfltr run` で実行アーカイブを生成してください")
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
    raise RunIdError(f"run_id が見つかりません: {raw!r}。`pyfltr list-runs` で有効な run_id を確認できます")


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
        slow_tests = tool_meta.get("slow_tests")
        if slow_tests:
            summary["slow_tests"] = slow_tests
        summaries.append(summary)
    return summaries
