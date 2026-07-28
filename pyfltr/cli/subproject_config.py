"""サブプロジェクト別configの解決。

`run_pipeline`から呼び出される。各サブプロジェクトの`pyproject.toml`を
`load_config(config_dir=cwd, for_subproject=True)`で個別解決し、`pyproject.toml`を持たない
サブプロジェクト（`Cargo.toml`単独・`*.csproj`単独等）は最近接祖先の設定を継承する。

`for_subproject=True`を渡すのは、設定ファイル不在の警告をサブプロジェクトのディレクトリ
基準で発行させないため（`.pre-commit-config.yaml`等はリポジトリルートに配置され、
`config_arg_template`による設定注入も起点cwd直下から解決される）。

`suppressed_warning_keys`には、ループ開始時点までに実際に発行済みのグローバル由来警告キー集合の
スナップショットを渡す。起点`config`（本モジュールが引数として受け取る、起点cwdで
既にロード済みのConfig）が保持する`warned_global_only_keys`を初期値とし、各サブプロジェクトの
`load_config`呼び出し後に戻り値の`warned_global_only_keys`を統合して更新する。
起点projectがglobalの不正値を正常値で上書きしていた場合、起点では警告が発行されないため
初期値には含まれないが、その後最初に不正値へ遭遇したサブプロジェクトが警告を発行し、
以降のサブプロジェクトへ抑止対象として引き継がれる。これにより実行全体で警告が
ちょうど1回だけ発行される（1個のロード結果だけを基準にする静的な積集合方式は、
複数の非上書きサブプロジェクトが並ぶ場合に重複警告を防げないため採らない。詳細は
「却下した代替案」参照）。
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

    `pyproject.toml`を持つサブプロジェクトは`load_config(config_dir=cwd, for_subproject=True)`で
    個別解決する。
    持たないサブプロジェクトは、真の祖先で`pyproject.toml`を持つサブプロジェクトのうち
    深度最深のものを継承元とする。該当祖先が無ければ起点configを継承する。
    いずれの経路でも起点と同一のCLIオーバーライド（`--jobs`・`--no-exclude` 等）を
    再適用してから返す（継承時は継承元の値をそのまま使う）。
    """
    subproject_configs: dict[pathlib.Path, pyfltr.config.config.Config] = {}
    already_warned: set[str] = set(config.warned_global_only_keys)
    # `pyproject.toml`を持つサブプロジェクトを先に解決して継承元候補にする（最近接判定に使うため）。
    pyproject_configs: dict[pathlib.Path, pyfltr.config.config.Config] = {}
    for sub in subprojects:
        if not (sub.cwd / "pyproject.toml").is_file():
            continue
        if sub.relative == ".":
            # 起点cwd自身は`config`引数として既にロード済みのため再ロードしない。
            # 再ロードすると、起点project自身の誤設定に由来する検証警告が起点ロード分と
            # 合わせて重複発行される（global由来キーの累積抑止は`already_warned`が担うが、
            # project由来の警告はこの累積の対象外のため別途の対処が要る）。
            pyproject_configs[sub.cwd] = config
            continue
        sub_config = pyfltr.config.config.load_config(
            config_dir=sub.cwd,
            for_subproject=True,
            suppressed_warning_keys=frozenset(already_warned),
        )
        already_warned |= sub_config.warned_global_only_keys
        pyproject_configs[sub.cwd] = sub_config
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
