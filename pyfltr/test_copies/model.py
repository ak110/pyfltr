import dataclasses

COPY_KINDS = {"literal", "constant", "text"}


@dataclasses.dataclass
class TestFunction:
    id: str
    line: int
    start: int
    end: int
    body_start: int
    checks: list[str] = dataclasses.field(default_factory=list)
    reason: str | None = None


def combine(values: list[str]) -> str:
    if not values or "unknown" in values:
        return "unknown"
    if "behavior" in values:
        return "behavior"
    if "text" in values:
        return "text"
    if "constant" in values:
        return "constant"
    return "literal" if all(value == "literal" for value in values) else "unknown"
