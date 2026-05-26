"""構造化出力用の引数注入仕様と適用ロジック。

`{command}-args`とは独立した経路で、ツールへ構造化出力（JSON等）の出力形式引数を強制注入する。
設定キー（例: `ruff-check-json`）がTrueのとき、対応する仕様で起動引数を組み立てる。
注入時はコマンドラインから`conflicts`に一致する既存引数を除去したうえで`inject`を追加する
（ruff / typosは重複指定でエラーになるため）。
"""

import dataclasses

import pyfltr.config.config


@dataclasses.dataclass(frozen=True)
class StructuredOutputSpec:
    """構造化出力用の引数注入仕様。"""

    inject: list[str]
    """注入する引数"""
    conflicts: list[str]
    """commandlineから除去する引数プレフィクス"""
    lint_only: bool = False
    """Trueのときfixモードでは注入しない"""


# 各ツールの構造化出力用引数。設定キー → 注入仕様のマッピング。
# 設定キー（例: "ruff-check-json"）がTrueのとき有効になる。
_STRUCTURED_OUTPUT_SPECS: dict[str, tuple[str, StructuredOutputSpec]] = {
    "ruff-check-json": (
        "ruff-check",
        StructuredOutputSpec(
            inject=["--output-format=json"],
            conflicts=["--output-format"],
        ),
    ),
    "pylint-json": (
        "pylint",
        StructuredOutputSpec(
            inject=["--output-format=json2"],
            conflicts=["--output-format"],
        ),
    ),
    "pyright-json": (
        "pyright",
        StructuredOutputSpec(
            inject=["--outputjson"],
            conflicts=["--outputjson"],
        ),
    ),
    "pytest-tb-line": (
        "pytest",
        StructuredOutputSpec(
            inject=["--tb=short"],
            conflicts=["--tb"],
        ),
    ),
    "shellcheck-json": (
        "shellcheck",
        StructuredOutputSpec(
            inject=["-f", "json"],
            conflicts=["-f"],
        ),
    ),
    "textlint-json": (
        "textlint",
        StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
            lint_only=True,
        ),
    ),
    "typos-json": (
        "typos",
        StructuredOutputSpec(
            inject=["--format=json"],
            conflicts=["--format"],
        ),
    ),
    "eslint-json": (
        "eslint",
        StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
        ),
    ),
    "biome-json": (
        "biome",
        StructuredOutputSpec(
            inject=["--reporter=github"],
            conflicts=["--reporter"],
        ),
    ),
}


def get_structured_output_spec(command: str, config: pyfltr.config.config.Config) -> StructuredOutputSpec | None:
    """コマンドに対応する構造化出力仕様を返す。無効化されていればNone。"""
    for config_key, entry in _STRUCTURED_OUTPUT_SPECS.items():
        cmd = entry[0]
        spec = entry[1]
        if cmd == command and config.values.get(config_key, False):
            return spec
    return None


def apply_structured_output(commandline: list[str], spec: StructuredOutputSpec) -> list[str]:
    """コマンドラインから衝突する引数を除去し、構造化出力引数を注入する。"""
    filtered: list[str] = []
    skip_next = False
    for i, arg in enumerate(commandline):
        if skip_next:
            skip_next = False
            continue
        matched = False
        for prefix in spec.conflicts:
            if arg == prefix:
                # "-f gcc" 形式: 次の引数もスキップ
                if i + 1 < len(commandline) and not commandline[i + 1].startswith("-"):
                    skip_next = True
                matched = True
                break
            if arg.startswith(f"{prefix}=") or (arg.startswith(prefix) and arg != prefix):
                # "--format=json" 形式 / "--outputjson" 形式
                matched = True
                break
        if not matched:
            filtered.append(arg)
    return [*filtered, *spec.inject]
