"""CLIとMCPの実行結果に添える案内。"""

import shlex


def build_summary_guidance(
    *,
    failure_present: bool,
    applied_fixes_present: bool,
    resolution_failed_present: bool = False,
    run_id: str | None,
    launcher_prefix: list[str] | None,
    subcommand: str | None,
    for_mcp: bool = False,
) -> list[str]:
    """summaryレコード向けの状況依存ガイドを英語で生成する。

    `summary.guidance`として次のいずれかを満たす場合のみ同梱する。

    - `commands_summary.needs_action`配下の`failed`/`resolution_failed`合計が1以上
    - `summary.applied_fixes`が非空（formatter/fix-stageが書き換えた）

    両方該当する場合は失敗時の案内に続けてformatter書き換えの注記1項目を追記する。
    アーカイブを参照する再実行と詳細取得の2項目は`run_id`がある場合だけ並べる。
    `resolution_failed_present`が真のときは、ツール解決失敗の対処（`tool-resolve`警告の本文と`hint`）を
    読む案内を失敗時の項目の先頭へ置く。解決失敗は再実行だけでは解消しないため。
    MCPの`run`ツールも本関数を呼び、`for_mcp=True`でMCPの操作名を使う。
    `run_id`と`launcher_prefix`が指定されていれば、起動コマンド表記と実run_idを埋め込む。
    `subcommand`は`--only-failed`の再実行例に埋め込む実行系サブコマンド名で、
    実際に起動されたサブコマンドをそのまま反映する。
    `launcher_prefix`・`subcommand`未指定時の既定値は`pyfltr`・`run`とする。
    """
    if not failure_present and not applied_fixes_present:
        return []
    launcher = shlex.join(launcher_prefix) if launcher_prefix else "pyfltr"
    subcommand_token = subcommand if subcommand else "run"
    items: list[str] = []
    if failure_present:
        if resolution_failed_present:
            items.append(
                "resolution_failed means the tool could not be started;"
                " follow the tool-resolve warning to install the tool or change its runner setting,"
                " then re-run."
            )
        items.append(
            "Inspect retry_commands to re-run only failing files."
            if for_mcp
            else "Inspect command.retry_command in failed command records to re-run only failing files."
        )
        if run_id is not None:
            items.append(
                f"Use the run tool with mode={subcommand_token!r}, only_failed=True to retry the failure set."
                if for_mcp
                else f"Use '{launcher} {subcommand_token} --only-failed' to retry the failure set in one step."
            )
        items.append(
            "diagnostic.fix == 'safe'/'unsafe'/'suggested' means auto-fixable; 'none' or omitted means manual fix needed."
        )
        if run_id is not None:
            items.append(
                f"Use show_run(run_id={run_id!r}) to inspect the run archive, then show_run_output for full per-command output."
                if for_mcp
                else f"Use '{launcher} show-run {run_id}' for full per-command output stored in the run archive."
            )
    if applied_fixes_present:
        items.append(
            "formatter/fix-stage rewrote files;"
            " re-running is not required because formatter rewrites are classified as no_issues by project policy."
        )
    return items
