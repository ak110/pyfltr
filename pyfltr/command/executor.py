import collections.abc
import contextlib
import pathlib
import threading

import pyfltr.command.core_
import pyfltr.command.targets
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.config.selection

_serial_group_locks: dict[str, threading.Lock] = {}
"""`serial_group`名をキーにした排他ロック辞書。"""

_serial_group_locks_registry_lock = threading.Lock()
"""`_serial_group_locks` 自体の生成を直列化するためのメタロック。"""


@contextlib.contextmanager
def serial_group_lock(group: str | None) -> collections.abc.Generator[None, None, None]:
    if group is None:
        yield
        return
    with _serial_group_locks_registry_lock:
        lock = _serial_group_locks.get(group)
        if lock is None:
            lock = threading.Lock()
            _serial_group_locks[group] = lock
    with lock:
        yield


def split_commands_for_execution(
    commands: list[str],
    config: pyfltr.config.model.Config,
    all_files: list[pathlib.Path],
    *,
    include_fix_stage: bool = False,
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config] | None = None,
) -> tuple[list[str], list[str], list[str]]:
    fixers: list[str] = []
    if include_fix_stage:
        fixers = pyfltr.config.selection.filter_fix_commands(commands, config, subproject_configs)

    formatters: list[str] = []
    linters_and_testers: list[str] = []
    for command in commands:
        if not pyfltr.config.selection.is_command_enabled_anywhere(command, config, subproject_configs):
            continue
        if config.commands[command].type == "formatter":
            formatters.append(command)
        else:
            linters_and_testers.append(command)

    def _estimate_time(c: str) -> float:
        info = config.commands[c]
        n = len(pyfltr.command.targets.filter_by_globs(all_files, info.target_globs()))
        return info.fixed_cost + info.per_file_cost * n

    linters_and_testers.sort(key=_estimate_time, reverse=True)
    return fixers, formatters, linters_and_testers


def has_no_candidate_targets(command: str, base_ctx: pyfltr.command.core_.ExecutionBaseContext) -> bool:
    contexts = [base_ctx]
    if command == "pytest" and base_ctx.pytest_base is not None:
        contexts.append(base_ctx.pytest_base)
    files: list[pathlib.Path] = []
    for context in contexts:
        files.extend(context.all_files)
        files.extend(context.external_files)
        for sub_files in context.subproject_files.values():
            files.extend(sub_files)
    if not files:
        return True
    globs: list[str] = []
    for config in [base_ctx.config, *base_ctx.subproject_configs.values()]:
        info = config.commands.get(command)
        if info is not None:
            globs.extend(info.target_globs())
        if command == "pytest":
            globs.extend(pyfltr.config.selection.pytest_fast_target_globs(config.values))
            if not base_ctx.exclude_always_targets:
                globs.extend(pyfltr.config.selection.pytest_always_target_globs(config.values))
    unique_files = list(dict.fromkeys(files))
    return not pyfltr.command.targets.filter_by_globs(unique_files, list(dict.fromkeys(globs)))
