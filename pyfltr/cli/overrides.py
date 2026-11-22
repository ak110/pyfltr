import pyfltr.cli.command_selection
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.run_options
import pyfltr.warnings_


def _flatten_comma_separated(values: list[str]) -> list[str]:
    flattened: list[str] = []
    for raw in values:
        for token in raw.split(","):
            item = token.strip()
            if item:
                flattened.append(item)
    return flattened


def apply_cli_overrides(
    config: pyfltr.config.model.Config, args: pyfltr.run_options.RunOptions, *, warn_unknown_command: bool = True
) -> None:
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
    if args.exclude_fence_under is not None:
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
                            source="cli",
                            message=f"`--{flag}={name}` は未知のコマンド名のため無視しました",
                            hint=pyfltr.cli.command_selection.format_unknown_command_message(name, config),
                        )
                    continue
                config.values[name] = enabled
