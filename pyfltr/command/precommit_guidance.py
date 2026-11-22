# Python 3.15以上で遅延importとなり、それ未満の版では通常のimportとして扱われる（PEP 810）。
__lazy_modules__ = ["psutil", "yaml"]

import logging
import os
import pathlib
import typing

import psutil
import yaml

import pyfltr.config.config
import pyfltr.config.model

logger = logging.getLogger(__name__)


def is_running_under_precommit() -> bool:
    return os.environ.get("PRE_COMMIT") == "1"


_GIT_PROCESS_NAMES: frozenset[str] = frozenset({"git", "git.exe"})


def is_invoked_from_git_commit() -> bool:
    try:
        proc = psutil.Process(os.getppid())
        candidates = [proc, *proc.parents()]
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    for candidate in candidates:
        try:
            name = candidate.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        if name in _GIT_PROCESS_NAMES:
            return True
    return False


def detect_pyfltr_hooks(config_dir: pathlib.Path) -> list[str]:
    config_path = config_dir / ".pre-commit-config.yaml"
    if not config_path.exists():
        return []

    with config_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        return []

    hook_ids: list[str] = []
    for repo in data.get("repos", []):
        if not isinstance(repo, dict):
            continue
        for hook in repo.get("hooks", []):
            if not isinstance(hook, dict):
                continue
            entry = hook.get("entry", "")
            if isinstance(entry, str) and "pyfltr" in entry:
                hook_id = hook.get("id", "")
                if hook_id:
                    hook_ids.append(hook_id)
    return hook_ids


def build_skip_value(
    config: pyfltr.config.model.Config,
    config_dir: pathlib.Path,
    command: typing.Literal["pre-commit", "prek"],
) -> str:
    skip_ids: list[str] = list(pyfltr.config.model.required_command_setting(config.values, command, "skip"))
    if pyfltr.config.model.required_command_setting(config.values, command, "auto-skip"):
        auto_ids = detect_pyfltr_hooks(config_dir)
        for hook_id in auto_ids:
            if hook_id not in skip_ids:
                skip_ids.append(hook_id)
    if skip_ids:
        logger.debug("%s SKIP対象: %s", command, ", ".join(skip_ids))
    return ",".join(skip_ids)
