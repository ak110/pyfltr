import dataclasses
import pathlib
import typing

import pyfltr.paths
import pyfltr.tools
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import extract_last_line, parse_with_pattern


def parse_errors(
    command: str,
    output: str,
    error_pattern: str | None = None,
    *,
    file_path_remap: dict[str, str] | None = None,
    path_base: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
) -> list[ErrorLocation]:

    def resolve(path: str) -> str:
        return pyfltr.paths.to_start_relative(path, path_base=path_base, start_cwd=start_cwd)

    info = pyfltr.tools.BUILTIN_COMMANDS.get(command)
    errors: list[ErrorLocation]
    if error_pattern is not None:
        errors = rebase_errors(parse_with_pattern(command, output, error_pattern), resolve)
    elif info is not None and info.path_resolving_parser is not None:
        errors = info.path_resolving_parser(output, resolve_path=resolve)
    elif info is not None and info.context_parser is not None:
        base = path_base or start_cwd or pathlib.Path.cwd()
        start = start_cwd or path_base or base
        errors = rebase_errors(info.context_parser(output, path_base=base, start_cwd=start), resolve)
    elif info is not None and info.parser is not None:
        errors = rebase_errors(info.parser(output), resolve)
    elif info is not None and info.diagnostic_pattern is not None:
        errors = rebase_errors(parse_with_pattern(command, output, info.diagnostic_pattern), resolve)
    else:
        return []
    return apply_file_path_remap(errors, file_path_remap, start_cwd=start_cwd)


def rebase_errors(errors: list[ErrorLocation], resolve: typing.Callable[[str], str]) -> list[ErrorLocation]:
    return [dataclasses.replace(error, file=resolve(error.file)) for error in errors]


def apply_file_path_remap(
    errors: list[ErrorLocation],
    remap: dict[str, str] | None,
    *,
    start_cwd: pathlib.Path | None = None,
) -> list[ErrorLocation]:
    if remap is None:
        return errors
    normalized_remap = dict(remap)
    for temporary_path, original_path in remap.items():
        normalized_remap[pyfltr.paths.to_start_relative(temporary_path, start_cwd=start_cwd)] = original_path
        normalized_remap[pyfltr.paths.normalize_separators(pathlib.Path(temporary_path).resolve())] = original_path
    return [
        dataclasses.replace(error, file=pyfltr.paths.to_start_relative(original_path, start_cwd=start_cwd))
        if (original_path := normalized_remap.get(normalize_remap_lookup_path(error.file))) is not None
        else error
        for error in errors
    ]


def normalize_remap_lookup_path(path: str) -> str:
    as_path = pathlib.Path(path)
    if as_path.is_absolute():
        return pyfltr.paths.normalize_separators(as_path.resolve())
    return pyfltr.paths.normalize_separators(path)


def sort_errors(errors: list[ErrorLocation], command_names: list[str]) -> list[ErrorLocation]:

    def sort_key(e: ErrorLocation) -> tuple[str, int, int, int]:
        cmd_index = pyfltr.tools.command_index(command_names, e.command)
        return (e.file, e.line, e.col or 0, cmd_index)

    return sorted(errors, key=sort_key)


def get_custom_parser_commands() -> set[str]:
    return {
        command
        for command, info in pyfltr.tools.BUILTIN_COMMANDS.items()
        if info.parser or info.context_parser or info.path_resolving_parser
    }


def parse_summary(command: str, output: str) -> str | None:
    parser = info.summary_parser if (info := pyfltr.tools.BUILTIN_COMMANDS.get(command)) else None
    if parser is not None:
        return parser(output)
    return extract_last_line(output)
