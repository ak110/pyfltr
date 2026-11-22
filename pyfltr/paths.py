import os
import pathlib


def normalize_separators(path: str | pathlib.Path) -> str:
    return str(path).replace("\\", "/")


def sanitize_command_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in name)
    return safe or "_"


def to_cwd_relative(path: str | pathlib.Path) -> str:
    as_path = pathlib.Path(path)
    if as_path.is_absolute():
        try:
            return normalize_separators(as_path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            return normalize_separators(path)
    return normalize_separators(path)


def to_start_relative(
    path: str | pathlib.Path,
    *,
    path_base: pathlib.Path | None = None,
    start_cwd: pathlib.Path | None = None,
) -> str:
    text = str(path)
    if not text or text.startswith("<"):
        return normalize_separators(text)
    start = start_cwd if start_cwd is not None else pathlib.Path.cwd()
    as_path = pathlib.Path(text)
    if not as_path.is_absolute():
        as_path = (path_base if path_base is not None else start) / as_path
    normalized = pathlib.Path(os.path.normpath(as_path))
    try:
        return normalize_separators(normalized.relative_to(pathlib.Path(os.path.normpath(start))))
    except ValueError:
        return normalize_separators(normalized)


def is_external_path(target: pathlib.Path, *, start_cwd: pathlib.Path) -> bool:
    if not target.is_absolute():
        return False
    try:
        target.resolve().relative_to(start_cwd.resolve())
    except (OSError, ValueError):
        return True
    return False


def to_match_key(path: str | pathlib.Path, *, start_cwd: pathlib.Path) -> str:
    return to_start_relative(path, path_base=start_cwd, start_cwd=start_cwd)


def is_same_location(left: str | pathlib.Path, right: str | pathlib.Path) -> bool:
    return _comparable_location(left) == _comparable_location(right)


def _comparable_location(path: str | pathlib.Path) -> str:
    try:
        resolved = pathlib.Path(path).resolve()
    except (OSError, RuntimeError):
        resolved = pathlib.Path(os.path.abspath(path))
    return os.path.normcase(str(resolved))
