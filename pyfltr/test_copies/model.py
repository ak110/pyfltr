"""言語間で共有する検査結果と値の分類。"""

import dataclasses

COPY_KINDS = {"literal", "constant", "text"}


@dataclasses.dataclass
class TestFunction:
    """テスト宣言の範囲と、比較の分類・例外理由を保持する。"""

    id: str
    line: int
    start: int
    end: int
    body_start: int
    checks: list[str] = dataclasses.field(default_factory=list)
    reason: str | None = None


def combine(values: list[str]) -> str:
    """比較に含まれる値の出自を統合する。"""
    if not values or "unknown" in values:
        return "unknown"
    if "behavior" in values:
        return "behavior"
    if "text" in values:
        return "text"
    if "constant" in values:
        return "constant"
    return "literal" if all(value == "literal" for value in values) else "unknown"
