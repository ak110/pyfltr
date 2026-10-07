"""CLIとMCPで共有するチェック実行の入力。"""

from __future__ import annotations

import collections.abc
import dataclasses
import pathlib
import typing


@dataclasses.dataclass
class RunOptions:
    """公開入力を変換した実行オプションとツール固有の追加引数。"""

    targets: list[pathlib.Path] = dataclasses.field(default_factory=list)
    commands: list[str] | None = None
    enable: list[str] | None = None
    disable: list[str] | None = None
    exclude_fence_under: list[str] | None = None
    verbose: bool = False
    exit_zero_even_if_formatted: bool = False
    ui: bool | None = None
    no_ui: bool | None = None
    no_fix: bool = False
    quiet: bool | None = None
    stream: bool = False
    shuffle: bool = False
    keep_ui: bool = False
    ci: bool = False
    output_format: str | None = None
    output_file: pathlib.Path | None = None
    human_readable: bool = False
    no_clear: bool = False
    no_exclude: bool = False
    no_gitignore: bool = False
    allow_external_paths: bool = False
    no_archive: bool = False
    no_cache: bool = False
    fail_fast: bool = False
    only_failed: bool = False
    from_run: str | None = None
    changed_since: str | None = None
    work_dir: pathlib.Path | None = None
    jobs: int | None = None
    version: bool = False
    subcommand: str = "run"
    format_source: str | None = None
    include_fix_stage: bool = False
    command_arguments: dict[str, str] = dataclasses.field(default_factory=dict)

    @classmethod
    def from_values(cls, values: collections.abc.Mapping[str, typing.Any]) -> RunOptions:
        """パース済み入力を一度変換し、ツール固有の追加引数を対応表へまとめる。"""
        names = {field.name for field in dataclasses.fields(cls)}
        options = cls(**{key: value for key, value in values.items() if key in names})
        options.command_arguments = {
            **options.command_arguments,
            **{
                key.removesuffix("_args").replace("_", "-"): value
                for key, value in values.items()
                if key.endswith("_args") and isinstance(value, str)
            },
        }
        return options

    def tool_arguments(self, command: str) -> str:
        """指定ツールへ追加する引数文字列を返す。"""
        return self.command_arguments.get(command, "")

    def retry_arguments(self, *, for_mcp: bool = False) -> list[str]:
        """同じ実行を再現するCLI引数を、オプション値から構築する。"""
        argv = [self.subcommand]
        transport_fields = {
            "verbose",
            "ui",
            "no_ui",
            "quiet",
            "stream",
            "keep_ui",
            "output_format",
            "output_file",
            "no_clear",
            "ci",
            "fail_fast",
            "only_failed",
            "from_run",
            "changed_since",
        }
        boolean_fields = {field.name for field in dataclasses.fields(self) if field.type in {"bool", "bool | None"}} - {
            "version",
            "include_fix_stage",
        }
        sequence_fields = {"commands", "enable", "disable", "exclude_fence_under"}
        value_fields = {"work_dir", "jobs", "from_run", "changed_since", "output_format", "output_file"}
        order = [
            "work_dir",
            "no_fix",
            "commands",
            "enable",
            "disable",
            "exclude_fence_under",
            "allow_external_paths",
            "no_exclude",
            "no_gitignore",
            "no_cache",
            "human_readable",
            "shuffle",
            "exit_zero_even_if_formatted",
            "jobs",
        ]
        names = order + [field.name for field in dataclasses.fields(self) if field.name not in order]
        for name in names:
            if for_mcp and name in transport_fields:
                continue
            value = getattr(self, name)
            option = "--" + name.replace("_", "-")
            if name in boolean_fields and value:
                argv.append(option)
            elif name == "quiet" and value is False:
                argv.append("--no-quiet")
            elif name == "commands" and value:
                argv.append(f"{option}={','.join(value)}")
            elif name in sequence_fields and value:
                argv.extend(f"{option}={item}" for item in value)
            elif name in value_fields and value is not None:
                argv.append(f"{option}={value}")
        argv.extend(f"--{command}-args={value}" for command, value in self.command_arguments.items() if value)
        if not for_mcp:
            argv.extend(str(target) for target in self.targets)
        return argv
