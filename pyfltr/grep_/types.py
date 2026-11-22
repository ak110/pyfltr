import dataclasses
import pathlib


@dataclasses.dataclass(frozen=True)
class MatchRecord:
    file: pathlib.Path
    line: int
    col: int
    end_col: int | None
    line_text: str
    match_text: str
    before_lines: list[str]
    after_lines: list[str]


@dataclasses.dataclass(frozen=True)
class FileMatchSummary:
    file: pathlib.Path
    count: int
    lines: list[int]


@dataclasses.dataclass(frozen=True)
class ReplaceRecord:
    file: pathlib.Path
    line: int
    col: int
    before_line: str
    after_line: str
    before_text: str
    after_text: str


@dataclasses.dataclass(frozen=True)
class FileChangeSummary:
    file: pathlib.Path
    count: int
    before_hash: str | None
    after_hash: str | None


@dataclasses.dataclass(frozen=True)
class ReplaceCommandMeta:
    replace_id: str | None
    dry_run: bool
    fixed_strings: bool
    pattern: str
    replacement: str
    encoding: str
