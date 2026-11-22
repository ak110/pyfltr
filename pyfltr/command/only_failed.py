from __future__ import annotations

import dataclasses
import pathlib
import typing

import pyfltr.paths


@dataclasses.dataclass(frozen=True)
class ToolTargets:
    mode: typing.Literal["fallback", "files"]
    files: tuple[pathlib.Path, ...]

    @classmethod
    def fallback_default(cls) -> ToolTargets:
        return cls(mode="fallback", files=())

    @classmethod
    def with_files(cls, files: typing.Iterable[pathlib.Path]) -> ToolTargets:
        return cls(mode="files", files=tuple(files))

    def resolve_files(self, all_files: list[pathlib.Path]) -> list[pathlib.Path]:
        if self.mode == "fallback":
            return all_files
        if self.mode == "files":
            selected = {pyfltr.paths.normalize_separators(p) for p in self.files}
            return [p for p in all_files if pyfltr.paths.normalize_separators(p) in selected]
        typing.assert_never(self.mode)
