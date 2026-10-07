"""構造化出力用の引数注入仕様と適用ロジック。

`{command}-args`とは独立した経路で、ツールへ構造化出力（JSON等）の出力形式引数を強制注入する。
設定キー（例: `ruff-check-json`）がTrueのとき、対応する仕様で起動引数を組み立てる。
注入時はコマンドラインから`conflicts`に一致する既存引数を除去したうえで`inject`を追加する
（ruff / typosは重複指定でエラーになるため）。
"""

import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.tools import StructuredOutputSpec

# 各ツールの構造化出力用引数。設定キー → 注入仕様のマッピング。
# 設定キー（例: "ruff-check-json"）がTrueのとき有効になる。


def get_structured_output_spec(command: str, config: pyfltr.config.model.Config) -> StructuredOutputSpec | None:
    """有効な構造化出力仕様をツール定義から返す。"""
    info = pyfltr.tools.BUILTIN_COMMANDS.get(command)
    if info is None or info.structured_output is None:
        return None
    config_key, spec = info.structured_output
    return spec if config.values.get(config_key, False) else None


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
