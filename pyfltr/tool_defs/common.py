"""複数の種類で共用するツール定義値。"""

from pyfltr.tool_types import StructuredOutputSpec

JS_COMMON_TARGETS: list[str] = [
    "*.js",
    "*.jsx",
    "*.mjs",
    "*.cjs",
    "*.ts",
    "*.tsx",
    "*.mts",
    "*.cts",
]

CARGO_SHORT_OUTPUT = StructuredOutputSpec(
    inject=["--message-format=short", "--color=never"],
    conflicts=["--message-format", "--color"],
    before_argument="--",
)
