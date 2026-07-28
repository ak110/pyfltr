"""CLIオプションによるconfig上書き適用。

`run_pipeline`とサブプロジェクトconfig読込経路から共通で呼び出される
`apply_cli_overrides`を担う。
"""

import argparse

import pyfltr.config.config
import pyfltr.warnings_


def _flatten_comma_separated(values: list[str]) -> list[str]:
    """複数回指定とカンマ区切りを平坦化し、空要素を除外する。"""
    flattened: list[str] = []
    for raw in values:
        for token in raw.split(","):
            item = token.strip()
            if item:
                flattened.append(item)
    return flattened


def apply_cli_overrides(
    config: pyfltr.config.config.Config, args: argparse.Namespace, *, warn_unknown_command: bool = True
) -> None:
    """CLIオプションによるconfig上書きを適用する。

    起点configとサブプロジェクト別configの双方へ同一に適用し、`--jobs`・`--no-exclude`・
    `--no-gitignore`・`--human-readable`・`--enable`・`--disable` の指定が一部サブプロジェクトにのみ
    反映される不整合を避ける。
    `--no-fix`・`--ci` は `config.values` ではなく `args` 側に作用するためここでは扱わない。

    `--enable` と `--disable` が同一コマンドに指定された場合は `--enable` を優先する。
    未知のコマンド名を指定された場合は当該指定を無視する。

    Args:
        config: 上書き対象のconfig。`config.values`を直接書き換える。
        args: パース済みのCLI引数。
        warn_unknown_command: 未知のコマンド名に対する警告を発行するか。
            モノレポでは本関数が起点1回とサブプロジェクトごとに1回ずつ呼ばれるため、
            既定の`True`で呼ぶのは起点だけとし、サブプロジェクトへの再適用
            （`resolve_subproject_configs`）は`False`を渡して値の反映のみ行う。
            CLI引数はサブプロジェクト間で共通であり、警告内容も同一になるため、
            発行を起点へ集約してもサブプロジェクト固有の情報は失われない。
    """
    if args.jobs is not None:
        config.values["jobs"] = args.jobs
    if args.no_exclude:
        config.values["exclude"] = []
        config.values["extend-exclude"] = []
    if args.no_gitignore:
        config.values["respect-gitignore"] = False
    if args.human_readable:
        for key in list(config.values):
            if key.endswith("-json") or key == "pytest-tb-line":
                config.values[key] = False
    if getattr(args, "exclude_fence_under", None) is not None:
        cli_values = _flatten_comma_separated(args.exclude_fence_under)
        existing = list(config.values.get("exclude-fence-under", []))
        config.values["exclude-fence-under"] = existing + [value for value in cli_values if value not in existing]
    for flag, enabled in (("disable", False), ("enable", True)):
        for raw in getattr(args, flag, None) or []:
            for token in raw.split(","):
                name = token.strip()
                if not name:
                    continue
                if name not in config.commands:
                    if warn_unknown_command:
                        pyfltr.warnings_.emit_warning(
                            source="cli", message=f"`--{flag}={name}` は未知のコマンド名のため無視します"
                        )
                    continue
                config.values[name] = enabled
