"""testersのツール登録。"""

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_defs.common import CARGO_SHORT_OUTPUT
from pyfltr.tool_types import BinToolSpec, CommandInfo, FunctionRef, StructuredOutputSpec

COMMANDS: dict[str, CommandInfo] = {
    "pytest": CommandInfo(
        type="tester",
        targets="*_test.py",
        fixed_cost=3.0,
        allows_external_paths=False,
        python_bin="pytest",
        context_parser=FunctionRef[..., list[ErrorLocation]]("pyfltr.parsing.pytest", "parse_pytest"),
        summary_parser=FunctionRef[[str], str | None]("pyfltr.parsing.pytest", "summarize_pytest"),
        structured_output=(
            "pytest-tb-line",
            StructuredOutputSpec(
                # 設定キー名は`--tb=line`を示唆するが、実際に注入するのは`--tb=short`である。
                # `--tb=short`はプロジェクト内フレームを含みつつ出力量を抑えられるため既定採用としており、
                # キー名は初期実装時の名残。設定互換性維持のため改名しない
                inject=["--tb=short"],
                conflicts=["--tb"],
            ),
        ),
        language="python",
        defaults={
            "pytest-tb-line": True,
            "pytest": False,
            "pytest-path": "",
            "pytest-runner": "python-runner",
            "pytest-args": [],
            "pytest-devmode": True,
            "pytest-fast": False,
            # fast選択時だけpytestの対象を置き換えるglob（文字列または配列）。
            # 非空ならpytest-fast=falseでもfastへ参加し、位置引数・差分指定に依らずプロジェクト全域から一致ファイルを選ぶ。
            "pytest-fast-targets": [],
            # 全実行でpytestの対象へ加えるglob（文字列または配列）。
            # 位置引数・差分指定に依らずプロジェクト全域から一致ファイルを選び、通常の対象との和集合を実行する。
            "pytest-always-targets": [],
        },
    ),
    # vitest のテストファイルパターン（pytest の *_test.py と同じ考え方）
    "vitest": CommandInfo(
        type="tester",
        fixed_cost=3.0,
        targets=[
            "*.test.js",
            "*.test.jsx",
            "*.test.ts",
            "*.test.tsx",
            "*.spec.js",
            "*.spec.jsx",
            "*.spec.ts",
            "*.spec.tsx",
            "*.test.mjs",
            "*.test.mts",
            "*.test.cjs",
            "*.test.cts",
            "*.spec.mjs",
            "*.spec.mts",
            "*.spec.cjs",
            "*.spec.cts",
        ],
        allows_external_paths=False,
        js_bin="vitest",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_vitest_json"),
        language="javascript",
        defaults={
            "vitest": False,
            "vitest-path": "",
            "vitest-runner": "js-runner",
            # vitestはrunサブコマンドが必須。また、pyfltrがtargets設定で限定したファイル群と
            # プロジェクト側のvitest include設定が交差せず対象ゼロになるケースでrc=1となり
            # failed扱いになるのを避けるため、--passWithNoTestsを既定に含める。
            "vitest-args": ["run", "--passWithNoTests"],
            "vitest-fast": False,
        },
        execution_kind="vitest",
    ),
    # Rust / .NETツール（tester）。pass-filenames=FalseでCrate / solution全体を対象とする。
    "cargo-test": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_cargo_test"),
        structured_output=(
            "cargo-test-structured",
            CARGO_SHORT_OUTPUT,
        ),
        type="tester",
        targets=["*.rs", "Cargo.toml"],
        serial_group="cargo",
        fixed_cost=3.0,
        allows_external_paths=False,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            "cargo-test-structured": True,
            "cargo-test": False,
            "cargo-test-path": "",
            "cargo-test-runner": "bin-runner",
            "cargo-test-version": "latest",
            "cargo-test-args": ["test"],
            "cargo-test-pass-filenames": False,
            "cargo-test-fast": False,
        },
    ),
    "dotnet-test": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_dotnet_test"),
        type="tester",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
        fixed_cost=5.0,
        allows_external_paths=False,
        bin_spec=BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
        language="dotnet",
        defaults={
            "dotnet-test": False,
            "dotnet-test-path": "",
            "dotnet-test-runner": "bin-runner",
            "dotnet-test-version": "latest",
            "dotnet-test-args": ["test", "--nologo"],
            "dotnet-test-pass-filenames": False,
            "dotnet-test-fast": False,
        },
    ),
}
