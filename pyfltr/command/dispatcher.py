import dataclasses
import pathlib
import random
import shlex
import time
import typing

import natsort

import pyfltr.command.cache_policy
import pyfltr.command.core_
import pyfltr.command.env
import pyfltr.command.glab
import pyfltr.command.invocation
import pyfltr.command.linter_fix
import pyfltr.command.lychee
import pyfltr.command.markdownlint_env
import pyfltr.command.only_failed
import pyfltr.command.precommit
import pyfltr.command.process
import pyfltr.command.pytest_impact
import pyfltr.command.runner
import pyfltr.command.slow_tests
import pyfltr.command.structured_output
import pyfltr.command.subproject_loop
import pyfltr.command.subprojects
import pyfltr.command.targets
import pyfltr.command.textlint_fix
import pyfltr.command.tool_resolution
import pyfltr.command.tool_version
import pyfltr.command.two_step.base
import pyfltr.command.two_step.prettier
import pyfltr.command.two_step.ruff
import pyfltr.command.vitest
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection
import pyfltr.parsing.pytest
import pyfltr.parsing.pytest_config
import pyfltr.paths
import pyfltr.run_options
import pyfltr.text.exclude_fence
import pyfltr.tool_types
import pyfltr.tools
import pyfltr.warnings_
from pyfltr.command.core_ import CacheContext, CommandResult, ExecutionContext, ExecutionParams

logger = __import__("logging").getLogger(__name__)


def _to_subproject_relative(
    target: pathlib.Path,
    *,
    subproject_cwd: pathlib.Path,
    start_cwd: pathlib.Path,
) -> str:
    abs_path = target if target.is_absolute() else (start_cwd / target)
    try:
        rel = abs_path.resolve().relative_to(subproject_cwd.resolve())
    except (OSError, ValueError):
        return pyfltr.paths.normalize_separators(target)
    return pyfltr.paths.normalize_separators(str(rel))


def _resolve_config_inject_path(start_cwd: pathlib.Path, candidates: list[str]) -> pathlib.Path | None:
    for name in candidates:
        candidate = start_cwd / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def _is_mask_target(command: str, target: pathlib.Path, headings: list[str], *, fix_stage: bool) -> bool:
    return (
        not fix_stage
        and command in {"textlint", "markdownlint"}
        and bool(headings)
        and target.suffix.lower() in {".md", ".markdown"}
    )


def _source_path(target: pathlib.Path, *, start_cwd: pathlib.Path) -> pathlib.Path:
    if target.is_absolute():
        return target
    return start_cwd / target


def _original_file_argument(
    target: pathlib.Path,
    *,
    subproject_cwd: pathlib.Path | None,
    start_cwd: pathlib.Path,
) -> str:
    if subproject_cwd is not None:
        return _to_subproject_relative(target, subproject_cwd=subproject_cwd, start_cwd=start_cwd)
    return str(target)


def _temporary_markdown_path(
    target: pathlib.Path,
    *,
    start_cwd: pathlib.Path,
    temporary_dir: pathlib.Path,
) -> pathlib.Path:
    source = _source_path(target, start_cwd=start_cwd)
    try:
        relative = source.resolve().relative_to(start_cwd.resolve())
    except (OSError, ValueError):
        relative = pathlib.Path("__external__", *source.parts[1:])
    return temporary_dir / relative


def _masked_markdown_arguments(
    targets: list[pathlib.Path],
    *,
    command: str,
    headings: list[str],
    fix_stage: bool,
    start_cwd: pathlib.Path,
    subproject_cwd: pathlib.Path | None,
    temporary_dir_factory: typing.Callable[[], pathlib.Path] | None,
) -> tuple[list[str], dict[str, str] | None]:
    file_args: list[str] = []
    remap: dict[str, str] = {}
    for target in targets:
        original_arg = _original_file_argument(target, subproject_cwd=subproject_cwd, start_cwd=start_cwd)
        if not _is_mask_target(command, target, headings, fix_stage=fix_stage):
            file_args.append(original_arg)
            continue
        if temporary_dir_factory is None:
            file_args.append(original_arg)
            continue
        temporary_path = _temporary_markdown_path(target, start_cwd=start_cwd, temporary_dir=temporary_dir_factory())
        source_path = _source_path(target, start_cwd=start_cwd)
        masked = pyfltr.text.exclude_fence.mask_fenced_blocks_under_headings(
            source_path.read_text(encoding="utf-8"),
            headings,
        )
        temporary_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(masked, encoding="utf-8")
        file_args.append(str(temporary_path))
        remap[str(temporary_path)] = str(target)
    return file_args, remap or None


def _user_overrides_config(config_flag: str, *arg_lists: list[str]) -> bool:
    equals_prefix = f"{config_flag}="
    for args in arg_lists:
        for arg in args:
            if arg == config_flag or arg.startswith(equals_prefix):
                return True
    return False


def _collect_targets(
    command: str,
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    source_files: list[pathlib.Path],
    *,
    start_cwd: pathlib.Path | None,
    pytest_fast_targets_active: bool,
    pytest_always_targets_active: bool,
) -> list[pathlib.Path]:
    command_info = config.commands[command]
    globs = command_info.target_globs()
    if pytest_fast_targets_active and command == "pytest":
        globs = pyfltr.config.selection.pytest_fast_target_globs(config.values) or globs
    targets: list[pathlib.Path] = pyfltr.command.targets.filter_by_globs(source_files, globs)
    if pytest_always_targets_active and command == "pytest":
        always_globs = pyfltr.config.selection.pytest_always_target_globs(config.values)
        selected = set(targets)
        targets.extend(f for f in pyfltr.command.targets.filter_by_globs(source_files, always_globs) if f not in selected)

    if not args.no_exclude:
        tool_excludes: list[str] = pyfltr.config.model.command_setting(config.values, command, "exclude", [])
        if tool_excludes:
            start_for_exclude = start_cwd if start_cwd is not None else pathlib.Path.cwd()
            targets = [
                t
                for t in targets
                if not pyfltr.command.targets.matches_exclude_patterns(
                    t,
                    tool_excludes,
                    base_dir=pyfltr.command.targets.exclude_base_dir(t, targets=args.targets, start_cwd=start_for_exclude),
                )
            ]

    if not command_info.allows_external_paths and not args.allow_external_paths:
        start_for_filter = start_cwd if start_cwd is not None else pathlib.Path.cwd()
        kept: list[pathlib.Path] = []
        for t in targets:
            if pyfltr.paths.is_external_path(t, start_cwd=start_for_filter):
                normalized_target = pyfltr.paths.normalize_separators(t)
                pyfltr.command.targets.emit_external_path_warning(command, normalized_target)
            else:
                kept.append(t)
        targets = kept

    return targets


def _prepare_execution_params(
    command: str,
    args: pyfltr.run_options.RunOptions,
    config: pyfltr.config.model.Config,
    all_files: list[pathlib.Path],
    *,
    fix_stage: bool,
    only_failed_targets: "pyfltr.command.only_failed.ToolTargets | None",
    subproject_cwd: pathlib.Path | None = None,
    cwd: pathlib.Path | None = None,
    uv_workspace_root: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
    temporary_dir_factory: typing.Callable[[], pathlib.Path] | None = None,
    pytest_fast_targets_active: bool = False,
    pytest_always_targets_active: bool = False,
) -> "ExecutionParams | CommandResult":
    command_info = config.commands[command]
    source_files = only_failed_targets.resolve_files(all_files) if only_failed_targets is not None else all_files
    targets = _collect_targets(
        command,
        args,
        config,
        source_files,
        start_cwd=start_cwd,
        pytest_fast_targets_active=pytest_fast_targets_active,
        pytest_always_targets_active=pytest_always_targets_active,
    )

    if args.shuffle and not fix_stage:
        random.shuffle(targets)
    else:
        targets = natsort.natsorted(targets, key=str)

    fix_mode = fix_stage
    fix_args: list[str] | None = None
    if fix_mode:
        fix_args = pyfltr.config.model.command_setting(config.values, command, "fix-args", None)

    # 対象ファイル0件ならこの後の実行自体が行われないため、ツールパス解決を省略する。
    # mise等のbin-runner解決はネットワークやプラットフォーム制約で失敗し得るため、
    # 解決不要な状況で副作用的な失敗を発生させないよう早期返却する。
    if not targets:
        return ExecutionParams(
            command_info=command_info,
            targets=targets,
            commandline_prefix=[],
            commandline=[],
            cache_commandline=[],
            additional_args=[],
            fix_mode=fix_mode,
            fix_args=fix_args,
            via_mise=False,
            effective_runner=None,
            runner_source=None,
            runner_fallback=None,
        )

    try:
        resolved = pyfltr.command.runner.build_commandline(
            command,
            config,
            allow_side_effects=True,
            cwd=cwd,
            uv_workspace_root=uv_workspace_root,
        )
        resolved = pyfltr.command.runner.ensure_mise_available(resolved, config, command=command, cwd=cwd)
        if resolved.markdownlint_environment is not None:
            pyfltr.command.markdownlint_env.ensure_environment(
                resolved.markdownlint_environment,
                timeout=pyfltr.config.model.resolve_command_timeout(config.values, command),
            )
        pyfltr.command.tool_version.ensure_package_manager_version(resolved, config, command, cwd=cwd)
    except ValueError as e:
        hint = e.hint if isinstance(e, pyfltr.command.runner.RunnerMismatchError) else None
        return pyfltr.command.tool_resolution.failed_resolution_result(
            command, command_info, str(e), files=len(targets), hint=hint
        )
    except FileNotFoundError as e:
        message = pyfltr.command.tool_resolution.format_tool_resolution_failure(command, str(e), config)
        return pyfltr.command.tool_resolution.failed_resolution_result(command, command_info, message, files=len(targets))
    commandline_prefix = resolved.commandline

    additional_args_str = args.tool_arguments(command)
    additional_args = shlex.split(additional_args_str) if additional_args_str else []

    commandline = pyfltr.command.invocation.build_invocation_argv(
        command,
        config,
        commandline_prefix,
        additional_args,
        fix_stage=fix_args is not None,
    )

    injected_config_path: pathlib.Path | None = None
    if command_info.config_arg_template and command_info.config_inject_candidates:
        user_args_list: list[str] = list(pyfltr.config.model.command_setting(config.values, command, "args", []))
        extend_args_list: list[str] = list(pyfltr.config.model.command_setting(config.values, command, "extend-args", []))
        config_flag = command_info.config_arg_template[0]
        if not _user_overrides_config(config_flag, user_args_list, extend_args_list, additional_args):
            start_for_inject = start_cwd if start_cwd is not None else pathlib.Path.cwd()
            config_path = _resolve_config_inject_path(start_for_inject, command_info.config_inject_candidates)
            if config_path is not None:
                injected_config_path = config_path
                injection = [tok.format(path=str(config_path)) for tok in command_info.config_arg_template]
                prefix_len = len(commandline_prefix)
                commandline = commandline[:prefix_len] + injection + commandline[prefix_len:]

    file_path_remap: dict[str, str] | None = None
    cache_commandline = list(commandline)

    if pyfltr.config.model.command_setting(config.values, command, "pass-filenames", True):
        start_for_mask = start_cwd if start_cwd is not None else pathlib.Path.cwd()
        file_args, file_path_remap = _masked_markdown_arguments(
            targets,
            command=command,
            headings=list(config.values.get("exclude-fence-under", [])),
            fix_stage=fix_stage,
            start_cwd=start_for_mask,
            subproject_cwd=subproject_cwd,
            temporary_dir_factory=temporary_dir_factory,
        )
        if file_path_remap is None and subproject_cwd is not None and start_cwd is not None:
            commandline.extend(_to_subproject_relative(t, subproject_cwd=subproject_cwd, start_cwd=start_cwd) for t in targets)
        else:
            commandline.extend(file_args)
        cache_commandline.extend(
            _original_file_argument(t, subproject_cwd=subproject_cwd, start_cwd=start_for_mask) for t in targets
        )

    via_mise = resolved.effective_runner == "mise" or resolved.executable == "mise"

    return ExecutionParams(
        command_info=command_info,
        targets=targets,
        commandline_prefix=commandline_prefix,
        commandline=commandline,
        cache_commandline=cache_commandline,
        additional_args=additional_args,
        fix_mode=fix_mode,
        fix_args=fix_args,
        via_mise=via_mise,
        effective_runner=resolved.effective_runner,
        runner_source=resolved.runner_source,
        runner_fallback=resolved.runner_fallback,
        file_path_remap=file_path_remap,
        injected_config_path=injected_config_path,
    )


def _prepare_cache_context(
    command: str,
    command_info: pyfltr.tool_types.CommandInfo,
    config: pyfltr.config.model.Config,
    commandline: list[str],
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_args: list[str] | None,
    cache_store: "pyfltr.command.cache_policy.CacheStore | None",
    target_base_cwd: pathlib.Path,
    config_base_cwd: pathlib.Path,
    injected_config_path: pathlib.Path | None,
    subproject_cwd: pathlib.Path | None = None,
) -> CacheContext | None:
    if cache_store is None or not command_info.cacheable or fix_args is not None:
        return None
    if not pyfltr.command.cache_policy.is_cacheable(command, config, additional_args):
        return None
    structured_spec = pyfltr.command.structured_output.get_structured_output_spec(command, config)
    key = cache_store.compute_key(
        command=command,
        commandline=commandline,
        fix_stage=False,
        structured_output=structured_spec is not None,
        target_files=targets,
        config_files=pyfltr.command.cache_policy.resolve_config_files(
            command,
            config,
            base=config_base_cwd,
            injected_config_path=injected_config_path,
        ),
        target_base_cwd=target_base_cwd,
        subproject_cwd=subproject_cwd,
    )
    return CacheContext(cache_store=cache_store, command=command, key=key)


def _run_plain_command(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    cache_context = _prepare_cache_context(
        request.command,
        request.params.command_info,
        request.ctx.config,
        request.params.cache_commandline,
        request.params.targets,
        request.params.additional_args,
        fix_args=request.params.fix_args,
        cache_store=request.ctx.cache_store,
        target_base_cwd=request.ctx.base.start_cwd,
        config_base_cwd=request.cwd if request.cwd is not None else request.ctx.base.start_cwd,
        injected_config_path=request.params.injected_config_path,
        subproject_cwd=request.ctx.subproject_cwd,
    )
    lint_confirmation_keys = (
        pyfltr.command.textlint_fix.lint_confirmation_keys(request) if request.command == "textlint" else {}
    )
    if cache_context is not None:
        cached_result = cache_context.lookup()
        if cached_result is not None:
            cached_result.target_files = list(request.params.targets)
            cached_result.files = len(request.params.targets)
            if lint_confirmation_keys:
                pyfltr.command.textlint_fix.record_lint_confirmations(request, lint_confirmation_keys, cached_result.output)
            return cached_result

    proc = (
        pyfltr.command.lychee.run_lychee(
            request.params.commandline,
            request.ctx.config,
            request.env,
            request.ctx.on_output,
            verbose=request.verbose,
            is_interrupted=request.ctx.is_interrupted,
            on_subprocess_start=request.ctx.on_subprocess_start,
            on_subprocess_end=request.ctx.on_subprocess_end,
            cwd=request.cwd,
        )
        if request.command == "lychee"
        else pyfltr.command.process.run_process(request)
    )
    returncode = proc.returncode

    output = proc.stdout.strip()
    if lint_confirmation_keys and not proc.timeout_exceeded and returncode in (0, 1):
        pyfltr.command.textlint_fix.record_lint_confirmations(request, lint_confirmation_keys, output)
    elapsed = time.perf_counter() - request.start_time
    errors = request.parse_errors(output, request.params.command_info.error_pattern)
    slow_tests: list[pyfltr.command.slow_tests.SlowTest] = []
    if request.command == "pytest":
        conflict_message = pyfltr.parsing.pytest_config.detect_pytest_config_conflict(output)
        if conflict_message is not None:
            pyfltr.warnings_.emit_warning(
                source="config-conflict",
                message=conflict_message,
                hint="採用された設定ファイルへ設定を集約するか、不要な設定ファイルを削除する",
            )
        slow_tests = pyfltr.command.slow_tests.parse_pytest_durations(output)

    result = CommandResult.from_process(
        process=proc,
        command=request.command,
        command_info=request.params.command_info,
        commandline=request.params.commandline,
        files=len(request.params.targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        slow_tests=slow_tests,
    )

    if request.command == "lychee" and proc.returncode == 2 and not proc.timeout_exceeded:
        classification = pyfltr.command.lychee.classify_failures(output)
        if classification is not None and classification[0]:
            result.severity = "warning"

    if cache_context is not None and returncode == 0:
        cache_context.store(result, run_id=request.ctx.cache_run_id)

    return result


def execute_command(
    command: str,
    args: pyfltr.run_options.RunOptions,
    ctx: ExecutionContext,
) -> CommandResult:
    if command == "pytest" and ctx.base.pytest_base is not None:
        ctx = dataclasses.replace(ctx, base=ctx.base.pytest_base)
    if pyfltr.command.subproject_loop.should_run_subproject_loop(command, ctx):
        result = pyfltr.command.subproject_loop.run_subproject_loop(
            command,
            args,
            ctx,
            dispatch_fn=_dispatch_command,
            disabled_skip_fn=_make_disabled_skip_result,
        )
    else:
        result = _dispatch_command(command, args, ctx)
    pyfltr.command.tool_resolution.maybe_emit_uv_missing_tool_warning(result)
    return result


def _make_disabled_skip_result(command: str, ctx: ExecutionContext) -> CommandResult:
    info = ctx.config.commands.get(command)
    assert info is not None, "サブプロジェクトループは CommandInfo 非Noneの経路でのみ実行される"
    result = CommandResult.from_run(
        command=command,
        command_info=info,
        commandline=[],
        returncode=None,
        output=(
            "サブプロジェクト設定で無効化されているためスキップしました。"
            f"実行する場合は対象のpyproject.tomlで `{command} = true` に設定してください。"
        ),
        files=0,
        elapsed=0.0,
    )
    result.not_applicable = True
    return result


def _pytest_impact_scope(args: pyfltr.run_options.RunOptions, ctx: ExecutionContext) -> list[pathlib.Path] | None:
    if (
        ctx.config.values.get("pytest-impact-selection") is not True
        or args.only_failed
        or bool(args.tool_arguments("pytest"))
        or ctx.only_failed_targets is not None
        or args.allow_external_paths
        or ctx.base.pytest_fast_targets_active
        or args.subcommand == "fast"
        or not pyfltr.config.model.command_setting(ctx.config.values, "pytest", "pass-filenames", True)
    ):
        return None
    if args.targets:
        return ctx.all_files
    start = ctx.base.start_cwd
    root_config = ctx.base.pytest_impact_root_config or ctx.config
    full_files = pyfltr.command.targets.expand_all_files([], root_config, start_cwd=start)
    if ctx.subproject_cwd is not None:
        buckets, _ = pyfltr.command.subprojects.classify_files_by_subproject(full_files, ctx.base.subprojects, start)
        full_files = buckets.get(ctx.effective_cwd.resolve(), [])
    return _collect_targets(
        "pytest",
        args,
        ctx.config,
        full_files,
        start_cwd=start,
        pytest_fast_targets_active=False,
        pytest_always_targets_active=not ctx.base.exclude_always_targets,
    )


def _run_pytest_impact(
    request: pyfltr.command.core_.ExecutionRequest,
    args: pyfltr.run_options.RunOptions,
    mandatory: list[pathlib.Path],
) -> CommandResult:
    params = request.params
    cwd = request.ctx.effective_cwd
    start = request.ctx.base.start_cwd
    argv = params.commandline[: -len(params.targets)]
    signature_args = argv[len(params.commandline_prefix) :]
    full = not args.targets and args.changed_since is None
    env = {**request.env, "TESTSCOPE": "off"}

    def run(targets: list[pathlib.Path], *, record: bool = False) -> CommandResult:
        options = ["--testscope-record", f"--rootdir={cwd}"] if record else []
        files = [_original_file_argument(t, subproject_cwd=request.ctx.subproject_cwd, start_cwd=start) for t in targets]
        run_params = dataclasses.replace(params, targets=targets, commandline=[*argv, *options, *files])
        run_env = {**env, "TESTSCOPE": "record"} if record else env
        result = _run_plain_command(dataclasses.replace(request, params=run_params, env=run_env))
        result.target_files = targets
        return result

    if any(arg.startswith("--testscope") for arg in signature_args):
        return run(params.targets)

    probe = pyfltr.command.pytest_impact.probe(
        params.commandline_prefix, params.effective_runner, cwd, env, signature_args, record=full
    )
    if probe.reason:
        _report_impact(f"{probe.reason}。範囲内の全件（{len(params.targets)}ファイル）を実行します。")
    elif full:
        with pyfltr.command.pytest_impact.recording(cwd, probe.signature, params.targets, start) as recording:
            if recording is not None:
                result = run(params.targets, record=True)
                if result.returncode == 0 and not result.timeout_exceeded:
                    recording.accept()
                elif result.returncode == 4 and "unrecognized arguments: --testscope-record" in result.output:
                    _report_impact("記録用プラグインが有効でないため、記録せずに全件を実行します。")
                    return run(params.targets)
                if recording.complete:
                    _report_impact(f"全件（{len(params.targets)}ファイル）の履歴を記録しました。")
                else:
                    _report_impact("全件の履歴を更新できなかったため、以前の履歴を保持します。")
                return result
        _report_impact("履歴を書き込めないため、記録せずに全件を実行します。")
    else:
        selected = pyfltr.command.pytest_impact.select_files(params.targets, mandatory, probe, start_cwd=start, cwd=cwd)
        if selected:
            _report_impact(f"影響選択で範囲内の{len(params.targets)}ファイル中{len(selected)}ファイルを実行します。")
            return run(selected)
        _report_impact("影響選択の対象がないため、範囲内の全件を実行します。")
    return run(params.targets)


def _report_impact(message: str) -> None:
    pyfltr.warnings_.emit_warning(source="pytest-impact-selection", message=message, hint="対応不要")


def _dispatch_command(
    command: str,
    args: pyfltr.run_options.RunOptions,
    ctx: ExecutionContext,
) -> CommandResult:
    config = ctx.config
    all_files = ctx.all_files

    impact_scope = _pytest_impact_scope(args, ctx) if command == "pytest" else None
    mandatory = (
        _collect_targets(
            command,
            args,
            config,
            all_files,
            start_cwd=ctx.base.start_cwd,
            pytest_fast_targets_active=False,
            pytest_always_targets_active=ctx.base.pytest_always_targets_active,
        )
        if impact_scope is not None
        else []
    )
    if impact_scope is not None:
        keys = {pyfltr.paths.to_match_key(t, start_cwd=ctx.base.start_cwd) for t in impact_scope}
        if any(pyfltr.paths.to_match_key(t, start_cwd=ctx.base.start_cwd) not in keys for t in mandatory):
            impact_scope = None

    params_or_error = _prepare_execution_params(
        command,
        args,
        config,
        impact_scope if impact_scope is not None else all_files,
        fix_stage=ctx.fix_stage,
        only_failed_targets=ctx.only_failed_targets,
        subproject_cwd=ctx.subproject_cwd,
        cwd=ctx.effective_cwd,
        uv_workspace_root=ctx.uv_workspace_root,
        start_cwd=ctx.base.start_cwd,
        temporary_dir_factory=ctx.base.ensure_temporary_directory,
        pytest_fast_targets_active=ctx.base.pytest_fast_targets_active,
        pytest_always_targets_active=ctx.base.pytest_always_targets_active
        or (impact_scope is not None and not args.targets and not ctx.base.exclude_always_targets),
    )
    if isinstance(params_or_error, CommandResult):
        return params_or_error
    params = params_or_error
    command_info = params.command_info
    targets = params.targets
    commandline = params.commandline
    fix_args = params.fix_args

    severity = pyfltr.config.model.resolve_severity(config.values, command)

    def _with_targets(result: CommandResult, *, preserve_targets: bool = False) -> CommandResult:
        if not preserve_targets:
            result.target_files = list(targets)
        result.effective_runner = params.effective_runner
        result.runner_source = params.runner_source
        result.runner_fallback = params.runner_fallback
        if severity == "warning":
            result.severity = severity
        return result

    if len(targets) <= 0:
        no_target_result = CommandResult.from_run(
            command=command,
            command_info=command_info,
            commandline=commandline,
            returncode=None,
            output="対象ファイルが見つかりません。",
            files=0,
            elapsed=0,
        )
        no_target_result.not_applicable = True
        return _with_targets(no_target_result)

    start_time = time.perf_counter()
    env = pyfltr.command.env.build_subprocess_env(
        config, command, via_mise=params.via_mise, effective_runner=params.effective_runner
    )

    if command == "pytest" and config.values.get("pytest-impact-selection") is True:
        env["TESTSCOPE"] = "off"

    request = pyfltr.command.core_.ExecutionRequest(
        command=command,
        params=params,
        ctx=ctx,
        env=env,
        start_time=start_time,
        verbose=args.verbose,
        cwd=ctx.effective_cwd,
        nodeid_base_cwd=ctx.effective_cwd,
    )
    if impact_scope is not None:
        return _with_targets(_run_pytest_impact(request, args, mandatory), preserve_targets=True)
    kind = command_info.execution_kind
    if command_info.execution_enabled_key is not None and not config[command_info.execution_enabled_key]:
        kind = "plain"
    if fix_args is not None and command_info.type != "formatter":
        kind = command_info.fix_execution_kind
    return _with_targets(_EXECUTORS[kind](request), preserve_targets=kind == "textlint-fix")


_EXECUTORS: dict[pyfltr.tool_types.ExecutionKind, typing.Callable[[pyfltr.command.core_.ExecutionRequest], CommandResult]] = {
    "precommit": pyfltr.command.precommit.execute_pre_commit,
    "glab": pyfltr.command.glab.execute_glab_ci_lint,
    "vitest": pyfltr.command.vitest.execute_vitest,
    "textlint-fix": pyfltr.command.textlint_fix.execute_textlint_fix,
    "linter-fix": pyfltr.command.linter_fix.execute_linter_fix,
    "ruff-format": pyfltr.command.two_step.ruff.execute_ruff_format_two_step,
    "prettier": pyfltr.command.two_step.prettier.execute_prettier_two_step,
    "check-write": pyfltr.command.two_step.base.execute_check_write_two_step,
    "plain": _run_plain_command,
}
