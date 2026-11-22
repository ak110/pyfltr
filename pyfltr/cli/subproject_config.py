from __future__ import annotations

import copy
import dataclasses
import pathlib

import pyfltr.cli.overrides
import pyfltr.command.subprojects
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.run_options


def _is_ancestor_path(ancestor: pathlib.Path, descendant: pathlib.Path) -> bool:
    if ancestor == descendant:
        return False
    try:
        descendant.relative_to(ancestor)
    except ValueError:
        return False
    return True


def resolve_subproject_configs(
    subprojects: list[pyfltr.command.subprojects.Subproject],
    config: pyfltr.config.model.Config,
    args: pyfltr.run_options.RunOptions,
) -> dict[pathlib.Path, pyfltr.config.model.Config]:
    subproject_configs: dict[pathlib.Path, pyfltr.config.model.Config] = {}
    already_warned: set[pyfltr.config.model.ConfigWarningEntry] = set(config.warned_global_only_entries)
    pyproject_configs: dict[pathlib.Path, pyfltr.config.model.Config] = {}
    for sub in subprojects:
        if not (sub.cwd / "pyproject.toml").is_file():
            continue
        if sub.relative == ".":
            pyproject_configs[sub.cwd] = config
            continue
        sub_config = pyfltr.config.config.load_config(
            config_dir=sub.cwd,
            for_subproject=True,
            suppressed_warning_entries=frozenset(already_warned),
        )
        already_warned |= sub_config.warned_global_only_entries
        pyproject_configs[sub.cwd] = sub_config
    for sub in subprojects:
        if sub.cwd in pyproject_configs:
            base_config = pyproject_configs[sub.cwd]
        else:
            ancestors = [cand for cand in pyproject_configs if cand != sub.cwd and _is_ancestor_path(cand, sub.cwd)]
            if ancestors:
                nearest = max(ancestors, key=lambda p: len(p.parts))
                base_config = pyproject_configs[nearest]
            else:
                base_config = config
        sub_config = dataclasses.replace(base_config, values=copy.deepcopy(base_config.values))
        pyfltr.cli.overrides.apply_cli_overrides(sub_config, args, warn_unknown_command=False)
        subproject_configs[sub.cwd] = sub_config
    return subproject_configs
