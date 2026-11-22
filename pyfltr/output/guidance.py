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
