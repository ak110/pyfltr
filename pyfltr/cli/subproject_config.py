"""サブプロジェクト別configの解決。

`run_pipeline`から呼び出される。各サブプロジェクトの`pyproject.toml`を
`load_config(config_dir=cwd)`で個別解決し、`pyproject.toml`を持たないサブプロジェクト
（`Cargo.toml`単独・`*.csproj`単独等）は最近接祖先の設定を継承する。
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import pathlib

import pyfltr.cli.overrides
import pyfltr.command.subprojects
import pyfltr.config.config


def _is_ancestor_path(ancestor: pathlib.Path, descendant: pathlib.Path) -> bool:
    """`ancestor`が`descendant`の真の祖先ディレクトリかを判定する（同一パスはFalse）。"""
    if ancestor == descendant:
        return False
    try:
        descendant.relative_to(ancestor)
    except ValueError:
        return False
    return True


def resolve_subproject_configs(
    subprojects: list[pyfltr.command.subprojects.Subproject],
    config: pyfltr.config.config.Config,
    args: argparse.Namespace,
) -> dict[pathlib.Path, pyfltr.config.config.Config]:
    """サブプロジェクト別configを解決して返す。

    `pyproject.toml`を持つサブプロジェクトは`load_config(config_dir=cwd)`で個別解決する。
    持たないサブプロジェクトは、真の祖先で`pyproject.toml`を持つサブプロジェクトのうち
    深度最深のものを継承元とする。該当祖先が無ければ起点configを継承する。
    いずれの経路でも起点と同一のCLIオーバーライド（`--jobs`・`--no-exclude` 等）を
    再適用してから返す（継承時は継承元の値をそのまま使う）。
    """
    subproject_configs: dict[pathlib.Path, pyfltr.config.config.Config] = {}
    # `pyproject.toml`を持つサブプロジェクトを先に解決して継承元候補にする（最近接判定に使うため）。
    pyproject_configs: dict[pathlib.Path, pyfltr.config.config.Config] = {}
    for sub in subprojects:
        if (sub.cwd / "pyproject.toml").is_file():
            pyproject_configs[sub.cwd] = pyfltr.config.config.load_config(config_dir=sub.cwd)
    for sub in subprojects:
        if sub.cwd in pyproject_configs:
            base_config = pyproject_configs[sub.cwd]
        else:
            # 最近接祖先探索: 真の祖先で`pyproject.toml`を持つサブプロジェクトのうち深度最深
            ancestors = [cand for cand in pyproject_configs if cand != sub.cwd and _is_ancestor_path(cand, sub.cwd)]
            if ancestors:
                nearest = max(ancestors, key=lambda p: len(p.parts))
                base_config = pyproject_configs[nearest]
            else:
                base_config = config  # 起点config
        sub_config = dataclasses.replace(base_config, values=copy.deepcopy(base_config.values))
        pyfltr.cli.overrides.apply_cli_overrides(sub_config, args)
        subproject_configs[sub.cwd] = sub_config
    return subproject_configs
