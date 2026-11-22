from __future__ import annotations

import typing

import pyfltr.diagnostics

if typing.TYPE_CHECKING:
    import pyfltr.parsing.entry


def is_publishable_column(error: pyfltr.diagnostics.ErrorLocation, col: int | None) -> bool:
    return col is not None and col >= 1 and error.command != "textlint"
