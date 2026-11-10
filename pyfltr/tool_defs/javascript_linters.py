"""javascript_lintersのツール登録。"""

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_defs.common import JS_COMMON_TARGETS
from pyfltr.tool_types import CommandInfo, FunctionRef, StructuredOutputSpec

COMMANDS: dict[str, CommandInfo] = {
    # JS/TS linter群はモダン順（後ろほど新しい）に並べる。
    "tsc": CommandInfo(
        diagnostic_pattern=r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\): "
        r"(?P<severity>error|warning) (?P<rule>TS\d+): (?P<message>.+)$",
        structured_output=("tsc-plain", StructuredOutputSpec(inject=["--pretty", "false"], conflicts=["--pretty"])),
        type="linter",
        targets=["*.ts", "*.tsx", "*.mts", "*.cts"],
        js_bin="tsc",
        pnpx_package="typescript",
        language="javascript",
        defaults={
            "tsc-plain": True,
            "tsc": False,
            "tsc-path": "",
            "tsc-runner": "js-runner",
            "tsc-args": ["--noEmit"],
            "tsc-pass-filenames": False,
            "tsc-fast": False,
        },
    ),
    "eslint": CommandInfo(
        type="linter",
        targets=[*JS_COMMON_TARGETS, "*.vue", "*.svelte"],
        fixed_cost=2.3,
        per_file_cost=0.05,
        js_bin="eslint",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_eslint_json"),
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_eslint_url"),
        structured_output=(
            "eslint-json",
            StructuredOutputSpec(
                inject=["--format", "json"],
                conflicts=["--format"],
            ),
        ),
        language="javascript",
        defaults={
            "eslint-json": True,
            "eslint": False,
            "eslint-path": "",
            "eslint-runner": "js-runner",
            # ESLint 9系以降でcompact / unix / tapなどのコアフォーマッタが除去されたため、
            # 構造化出力はeslint-json設定により_STRUCTURED_OUTPUT_SPECS経由で注入する。
            "eslint-args": [],
            "eslint-fast": False,
            # fixモード時に通常argsの後に追加する引数。eslintは--fixでautofixする。
            "eslint-fix-args": ["--fix"],
        },
    ),
    "biome": CommandInfo(
        type="linter",
        targets=[*JS_COMMON_TARGETS, "*.json", "*.jsonc", "*.css"],
        js_bin="biome",
        pnpx_package="@biomejs/biome",
        diagnostic_pattern=r"::(?P<severity>error|warning|notice)\s+(?:[^:]*?title=(?P<rule>[^,]+))?"
        r"[^:]*?file=(?P<file>[^,]+)"
        r"[^:]*?(?<![A-Za-z])line=(?P<line>\d+)"
        r"(?:[^:]*?endLine=(?P<end_line>\d+))?"
        r"[^:]*?(?<![A-Za-z])col=(?P<col>\d+)"
        r"(?:[^:]*?endColumn=(?P<end_col>\d+))?"
        r"[^:]*?::(?P<message>.+)",
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_biome_url"),
        structured_output=(
            "biome-json",
            StructuredOutputSpec(
                inject=["--reporter=github"],
                conflicts=["--reporter"],
            ),
        ),
        language="javascript",
        defaults={
            "biome-json": True,
            "biome": False,
            "biome-path": "",
            "biome-runner": "js-runner",
            # "check"サブコマンドは共通argsに置く。--reporter=githubはbiome-json設定
            # により_STRUCTURED_OUTPUT_SPECS経由で注入する。
            "biome-args": ["check"],
            "biome-fast": True,
            # fixモード時に通常argsの後に追加する引数。
            # ruffの`--unsafe-fixes`採用方針と揃え、safe/unsafe両方のfixを自動適用する。
            # biomeのseverityは各ルールの既定値で決まり、fixのsafe/unsafeとは独立である。
            # severity infoの診断は`::notice`として出力されるが、
            # biome公式設計でinfoは終了コードに影響しない。
            # 個別ルールをCIの失敗対象に変更したい場合は`biome.json`の`linter.rules.*`で
            # severityを上げる（pyfltr側はinfoを失敗扱いに昇格させない）。
            "biome-fix-args": ["--write", "--unsafe"],
        },
    ),
    "oxlint": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_oxlint_json"),
        structured_output=("oxlint-structured", StructuredOutputSpec(inject=["--format=json"], conflicts=["--format", "-f"])),
        type="linter",
        targets=[*JS_COMMON_TARGETS, "*.vue", "*.svelte"],
        fixed_cost=0.7,
        js_bin="oxlint",
        pnpx_package="oxlint",
        language="javascript",
        defaults={
            "oxlint-structured": True,
            # -- js-runner対応ツール（追加分） --
            "oxlint": False,
            "oxlint-path": "",
            "oxlint-runner": "js-runner",
            "oxlint-args": [],
            "oxlint-fast": True,
        },
    ),
}
