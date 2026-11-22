import concurrent.futures
import dataclasses
import pathlib
import typing

import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.tool_parallelism
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.run_options
import pyfltr.warnings_
from pyfltr.command.core_ import CommandResult, ExecutionContext


def should_run_subproject_loop(command: str, ctx: ExecutionContext) -> bool:
    if ctx.subproject_cwd is not None:
        return False
    subprojects = ctx.base.subprojects
    if len(subprojects) < 2:
        return False
    info = ctx.config.commands.get(command)
    if info is None:
        return False
    default_aware = info.subproject_aware
    return pyfltr.config.model.resolve_subproject_aware(ctx.config.values, command, default_aware)


def _run_one(
    command: str,
    args: pyfltr.run_options.RunOptions,
    ctx: ExecutionContext,
    sub: typing.Any,
    sub_config: typing.Any,
    *,
    dispatch_fn: typing.Callable[[str, pyfltr.run_options.RunOptions, ExecutionContext], CommandResult],
) -> CommandResult:
    sub_base = dataclasses.replace(
        ctx.base,
        config=sub_config,
        pytest_impact_root_config=ctx.config if command == "pytest" else None,
    )
    sub_ctx = dataclasses.replace(ctx, base=sub_base, subproject_cwd=sub.cwd)
    sub_result = dispatch_fn(command, args, sub_ctx)
    if sub_result.output:
        sub_result.output = f"# subproject: {sub.relative}\n{sub_result.output}"
    return sub_result


def resolve_subproject_workers(command: str, ctx: ExecutionContext, cwds: typing.Sequence[typing.Any]) -> int:
    info = ctx.config.commands.get(command)
    if info is not None and not info.subproject_concurrent:
        return 1
    values = ctx.config.values
    budget = values.get("jobs", 1)
    if not isinstance(budget, int) or budget < 1:
        budget = 1
    tool_workers = 1
    for cwd in cwds:
        tool_workers = max(
            tool_workers,
            pyfltr.command.tool_parallelism.estimate_tool_workers(command, values, cwd),
        )
    return max(1, budget // tool_workers)


def run_subproject_loop(
    command: str,
    args: pyfltr.run_options.RunOptions,
    ctx: ExecutionContext,
    *,
    dispatch_fn: typing.Callable[[str, pyfltr.run_options.RunOptions, ExecutionContext], CommandResult],
    disabled_skip_fn: typing.Callable[[str, ExecutionContext], CommandResult],
) -> CommandResult:
    with pyfltr.warnings_.suppress_duplicates():
        base = ctx.base
        start_enabled = base.config.values.get(command) is True
        subproject_results: list[CommandResult] = []
        skipped_by_config = False
        targets: list[tuple[typing.Any, typing.Any]] = []
        for sub in _subprojects_with_start_cwd(base.subprojects, base.start_cwd, base.subproject_files):
            sub_files = base.subproject_files.get(sub.cwd, [])
            if not sub_files:
                continue
            sub_config = base.subproject_configs.get(sub.cwd, base.config)
            if sub_config.values.get(command) is not True:
                skipped_by_config = True
                continue
            targets.append((sub, sub_config))

        if targets:
            workers = min(len(targets), resolve_subproject_workers(command, ctx, [sub.cwd for sub, _ in targets]))
            ordered = sorted(targets, key=lambda item: item[0].relative)
            if workers <= 1:
                subproject_results.extend(
                    [_run_one(command, args, ctx, sub, sub_config, dispatch_fn=dispatch_fn) for sub, sub_config in ordered]
                )
            else:
                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers,
                    initializer=pyfltr.warnings_.adopt_suppression_state,
                    initargs=(pyfltr.warnings_.current_suppression_state(),),
                ) as executor:
                    futures = [
                        executor.submit(_run_one, command, args, ctx, sub, sub_config, dispatch_fn=dispatch_fn)
                        for sub, sub_config in ordered
                    ]
                    subproject_results.extend([future.result() for future in futures])

        info = ctx.config.commands.get(command)
        external_files = base.external_files
        if external_files and info is not None and start_enabled:
            relevant_external = pyfltr.command.targets.filter_by_globs(external_files, info.target_globs())
            if relevant_external:
                if info.allows_external_paths or args.allow_external_paths:
                    ext_base = dataclasses.replace(
                        base, all_files=relevant_external, subprojects=[], subproject_files={}, external_files=[]
                    )
                    ext_ctx = dataclasses.replace(ctx, base=ext_base, subproject_cwd=None)
                    ext_result = dispatch_fn(command, args, ext_ctx)
                    if ext_result.output:
                        ext_result.output = f"# external paths\n{ext_result.output}"
                    subproject_results.append(ext_result)
                else:
                    for t in relevant_external:
                        normalized_target = pyfltr.paths.normalize_separators(t)
                        pyfltr.command.targets.emit_external_path_warning(command, normalized_target)

        if subproject_results:
            if len(subproject_results) == 1:
                return subproject_results[0]
            return CommandResult.merge(subproject_results)

        if skipped_by_config or not start_enabled:
            return disabled_skip_fn(command, ctx)
        empty_base = dataclasses.replace(base, all_files=[], subprojects=[], subproject_files={}, external_files=[])
        return dispatch_fn(command, args, dataclasses.replace(ctx, base=empty_base))


def _subprojects_with_start_cwd(
    subprojects: list[pyfltr.command.subprojects.Subproject],
    start_cwd: pathlib.Path,
    subproject_files: dict[pathlib.Path, list[pathlib.Path]],
) -> list[pyfltr.command.subprojects.Subproject]:
    start_real = start_cwd.resolve()
    if not subproject_files.get(start_real) or any(sub.cwd == start_real for sub in subprojects):
        return list(subprojects)
    return [*subprojects, pyfltr.command.subprojects.Subproject(cwd=start_real, relative=".")]
