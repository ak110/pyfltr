from __future__ import annotations

import pathlib
import typing

import pyfltr.command.core_
import pyfltr.config.model

_EXTERNAL_REF_ARGS: frozenset[str] = frozenset({"--config", "--ignore-path"})


class CacheStore(typing.Protocol):
    def compute_key(
        self,
        *,
        command: str,
        commandline: list[str],
        fix_stage: bool,
        structured_output: bool,
        target_files: list[pathlib.Path],
        config_files: list[pathlib.Path],
        target_base_cwd: pathlib.Path | None = None,
        subproject_cwd: pathlib.Path | None = None,
    ) -> str: ...

    def get(self, command: str, key: str) -> pyfltr.command.core_.CommandResult | None: ...

    def put(self, command: str, key: str, result: pyfltr.command.core_.CommandResult, *, run_id: str | None) -> None: ...


def is_cacheable(
    command: str,
    config: pyfltr.config.model.Config,
    additional_args: list[str],
) -> bool:
    info = config.commands.get(command)
    if info is None or not info.cacheable:
        return False
    for arg in additional_args:
        if arg in _EXTERNAL_REF_ARGS:
            return False
        for ref in _EXTERNAL_REF_ARGS:
            if arg.startswith(f"{ref}="):
                return False
    return True


def resolve_config_files(
    command: str,
    config: pyfltr.config.model.Config,
    base: pathlib.Path | None = None,
    *,
    injected_config_path: pathlib.Path | None = None,
) -> list[pathlib.Path]:
    info = config.commands.get(command)
    if info is None or not info.config_files:
        return []
    root = base if base is not None else pathlib.Path.cwd()
    if injected_config_path is None:
        return [root / name for name in info.config_files]
    injected_names = set(info.config_inject_candidates)
    implicit_files = [root / name for name in info.config_files if name not in injected_names]
    return [injected_config_path, *implicit_files]
