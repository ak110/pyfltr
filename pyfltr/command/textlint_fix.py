import dataclasses
import json
import pathlib
import shlex
import time

import pyfltr.command.core_
import pyfltr.command.process
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.tools
from pyfltr.command import step_result
from pyfltr.command.core_ import CommandResult
from pyfltr.command.invocation import build_invocation_argv
from pyfltr.command.snapshot import (
    changed_files,
    snapshot_file_digests,
    snapshot_file_texts,
    warn_protected_identifier_corruption,
)

logger = __import__("logging").getLogger(__name__)


def _fix_cache_keys(
    request: pyfltr.command.core_.ExecutionRequest, argv: list[str], targets: list[pathlib.Path]
) -> dict[pathlib.Path, str]:
    store = request.ctx.cache_store
    prefix_len = len(request.params.commandline_prefix)
    if store is None or "--fix" not in argv[prefix_len:] or not set(argv[prefix_len:]) <= {"--fix", "--cache", "--no-cache"}:
        return {}
    cwd = request.cwd or request.ctx.base.start_cwd
    configs = [cwd / name for name in request.params.command_info.config_files]
    if request.params.injected_config_path is not None:
        configs.append(request.params.injected_config_path)
    if any(path.is_file() for path in configs if path.suffix in {".js", ".cjs"}):
        return {}
    common_key = store.compute_key(
        command=request.command,
        commandline=argv,
        fix_stage=True,
        structured_output=True,
        target_files=[],
        config_files=configs,
        subproject_cwd=cwd,
    )
    return {
        target: store.compute_key(
            command=request.command,
            commandline=[common_key],
            fix_stage=True,
            structured_output=True,
            target_files=[target],
            config_files=[],
            target_base_cwd=request.ctx.base.start_cwd,
        )
        for target in targets
    }


def _read_fix_report(output: str, cwd: pathlib.Path) -> tuple[set[pathlib.Path], str]:
    clean: set[pathlib.Path] = set()
    diagnostics: list[str] = []
    for line in output.split("\n"):
        try:
            results = json.loads(line)
        except ValueError:
            if not line.lstrip().startswith("["):
                diagnostics.append(line)
            continue
        if not isinstance(results, list):
            continue
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("filePath"), str):
                continue
            path = (cwd / item["filePath"]).resolve()
            if all(item.get(key) == [] for key in ("messages", "applyingMessages", "remainingMessages")):
                clean.add(path)
            for key in ("applyingMessages", "remainingMessages"):
                messages = item.get(key)
                if isinstance(messages, list):
                    for msg in messages:
                        if isinstance(msg, dict):
                            diagnostics.append(
                                f"{item['filePath']}:{msg.get('line')}:{msg.get('column')}: "
                                f"{msg.get('message')} ({msg.get('ruleId')})"
                            )
    return clean, "\n".join(diagnostics)


def lint_confirmation_keys(request: pyfltr.command.core_.ExecutionRequest) -> dict[pathlib.Path, str]:
    config = request.ctx.config
    lint_argv = build_invocation_argv(
        request.command, config, request.params.commandline_prefix, request.params.additional_args, fix_stage=False
    )
    if lint_argv[len(request.params.commandline_prefix) :] not in (
        ["--format", "json"],
        ["--cache", "--format", "json"],
        ["--no-cache", "--format", "json"],
    ):
        return {}
    fix_argv = build_invocation_argv(
        request.command, config, request.params.commandline_prefix, request.params.additional_args, fix_stage=True
    )
    return {target: key + "-lint" for target, key in _fix_cache_keys(request, fix_argv, request.params.targets).items()}


def record_lint_confirmations(
    request: pyfltr.command.core_.ExecutionRequest, keys: dict[pathlib.Path, str], output: str
) -> None:
    store = request.ctx.cache_store
    if store is None or not keys:
        return
    try:
        results = json.loads(output)
    except ValueError:
        return
    if not isinstance(results, list):
        return
    cwd = request.cwd or request.ctx.base.start_cwd
    clean = {
        (cwd / item["filePath"]).resolve()
        for item in results
        if isinstance(item, dict) and isinstance(item.get("filePath"), str) and item.get("messages") == []
    }
    for target, key in lint_confirmation_keys(request).items():
        if key == keys.get(target) and (request.ctx.base.start_cwd / target).resolve() in clean:
            _store_clean_marker(request, key)


def _store_clean_marker(request: pyfltr.command.core_.ExecutionRequest, key: str) -> None:
    store = request.ctx.cache_store
    if store is not None:
        marker = CommandResult.from_run(
            command=request.command,
            command_type="linter",
            commandline=[],
            returncode=0,
            files=1,
            output="",
            elapsed=0,
        )
        store.put(request.command, key, marker, run_id=request.ctx.cache_run_id)


def execute_textlint_fix(request: pyfltr.command.core_.ExecutionRequest) -> pyfltr.command.core_.CommandResult:
    if request.cwd is not None and request.ctx.base.start_cwd is not None:
        target_strs = [
            _relative_to_cwd(t, cwd=request.cwd, start_cwd=request.ctx.base.start_cwd) for t in request.params.targets
        ]
    else:
        target_strs = [str(t) for t in request.params.targets]

    step1_argv = build_invocation_argv(
        request.command,
        request.ctx.config,
        request.params.commandline_prefix,
        request.params.additional_args,
        fix_stage=True,
    )
    targets = request.params.targets
    keys = _fix_cache_keys(request, step1_argv, targets)
    store = request.ctx.cache_store
    lint_keys = lint_confirmation_keys(request) if keys else {}
    pending = [
        (target, token)
        for target, token in zip(targets, target_strs, strict=True)
        if store is None
        or target not in lint_keys
        or store.get(request.command, keys[target]) is None
        or store.get(request.command, lint_keys[target]) is None
    ]
    if not pending:
        return CommandResult.from_run(
            command=request.command,
            command_type="linter",
            commandline=[],
            returncode=0,
            files=0,
            output="",
            elapsed=time.perf_counter() - request.start_time,
            errors=[],
        )
    fix_targets = [target for target, _ in pending]
    report = bool(lint_keys)
    step1_commandline = [*step1_argv, *(["--format", "json"] if report else []), *(token for _, token in pending)]
    digests_before = snapshot_file_digests(fix_targets, base_cwd=request.ctx.base.start_cwd)
    protected_identifiers: list[str] = list(request.ctx.config.values.get("textlint-protected-identifiers", []))
    contents_before: dict[pathlib.Path, str] = (
        snapshot_file_texts(fix_targets, base_cwd=request.ctx.base.start_cwd) if protected_identifiers else {}
    )

    if request.verbose and request.ctx.on_output is not None:
        request.ctx.on_output(f"commandline: {shlex.join(step1_commandline)}\n")

    step1_request = dataclasses.replace(request, verbose=False)
    if report:
        step1_request = dataclasses.replace(
            step1_request,
            ctx=dataclasses.replace(request.ctx, on_output=None),
        )
    step1_proc = pyfltr.command.process.run_process(step1_request, step1_commandline)
    clean, step1_output = (
        _read_fix_report(step1_proc.stdout, request.cwd or request.ctx.base.start_cwd) if report else (set(), step1_proc.stdout)
    )
    if report and step1_output and request.ctx.on_output is not None:
        request.ctx.on_output(step1_output + "\n")
    digests_after_step1 = snapshot_file_digests(fix_targets, base_cwd=request.ctx.base.start_cwd)
    step1_changed = digests_after_step1 != digests_before

    if protected_identifiers and step1_changed:
        warn_protected_identifier_corruption(
            contents_before,
            snapshot_file_texts(fix_targets, base_cwd=request.ctx.base.start_cwd),
            protected_identifiers,
        )

    step2_commandline: list[str] = [
        *build_invocation_argv(
            request.command,
            request.ctx.config,
            request.params.commandline_prefix,
            request.params.additional_args,
            fix_stage=False,
        ),
        *target_strs,
    ]
    steps = [step_result.classify(step1_proc.returncode, timeout=step1_proc.timeout_exceeded, changed=step1_changed)]
    procs = [step1_proc]
    if request.ctx.base.fail_fast and step1_proc.returncode != 0 and not step1_proc.timeout_exceeded:
        if request.verbose and request.ctx.on_output is not None:
            request.ctx.on_output(f"commandline: {shlex.join(step2_commandline)}\n")
        step2_proc = pyfltr.command.process.run_process(dataclasses.replace(request, verbose=False), step2_commandline)
        procs.append(step2_proc)
        steps.append(step_result.classify(step2_proc.returncode, timeout=step2_proc.timeout_exceeded))

    output = "\n".join([step1_output, *(proc.stdout for proc in procs[1:])]).strip()
    elapsed = time.perf_counter() - request.start_time

    errors = request.parse_errors(procs[1].stdout.strip(), request.params.command_info.error_pattern) if len(procs) > 1 else []

    timeout_exceeded = any(proc.timeout_exceeded for proc in procs)
    combined = step_result.combine(*steps)
    step_failed = combined.failed
    returncode = combined.returncode
    result_command_type = "formatter" if combined.state is step_result.StepState.CHANGED else "linter"

    result = CommandResult.from_run(
        command=request.command,
        command_type=result_command_type,
        commandline=step2_commandline if len(procs) > 1 else step1_commandline,
        returncode=returncode,
        files=len(targets if len(procs) > 1 else fix_targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
        timeout_exceeded=timeout_exceeded,
        retry_count=sum(proc.retry_count for proc in procs),
    )
    result.target_files = list(targets if len(procs) > 1 else fix_targets)
    if not step_failed and step1_changed:
        result.fixed_files = changed_files(digests_before, digests_after_step1)
    if store is not None and clean and not step1_proc.timeout_exceeded and step1_proc.returncode in (0, 1):
        current_keys = _fix_cache_keys(request, step1_argv, fix_targets)
        for target in fix_targets:
            if (
                (request.ctx.base.start_cwd / target).resolve() in clean
                and digests_before[target]
                and digests_before[target] == digests_after_step1[target]
                and current_keys.get(target) == keys[target]
            ):
                _store_clean_marker(request, keys[target])
    return result


def _relative_to_cwd(target: pathlib.Path, *, cwd: pathlib.Path, start_cwd: pathlib.Path) -> str:
    abs_path = target if target.is_absolute() else (start_cwd / target)
    try:
        rel = abs_path.resolve().relative_to(cwd.resolve())
    except (OSError, ValueError):
        return pyfltr.paths.normalize_separators(target)
    return pyfltr.paths.normalize_separators(rel)
