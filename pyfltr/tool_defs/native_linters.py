"""native_lintersのツール登録。"""

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_defs.common import CARGO_SHORT_OUTPUT
from pyfltr.tool_types import BinToolSpec, CommandInfo, FunctionRef, StructuredOutputSpec

COMMANDS: dict[str, CommandInfo] = {
    # Rust / .NETツール（linter）。pass-filenames=FalseでCrate / solution全体を対象とする。
    "cargo-clippy": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_cargo_clippy"),
        structured_output=(
            "cargo-clippy-structured",
            CARGO_SHORT_OUTPUT,
        ),
        type="linter",
        targets=["*.rs", "Cargo.toml"],
        serial_group="cargo",
        fixed_cost=3.0,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            "cargo-clippy-structured": True,
            "cargo-clippy": False,
            "cargo-clippy-path": "",
            "cargo-clippy-runner": "bin-runner",
            "cargo-clippy-version": "latest",
            # args は lint / fix 両モードで共通の前半部分。trailing flag (-- -D warnings)
            # は lint-args / fix-args の双方に重複して置き、--fix 時には `--fix` を
            # 中間に挿入できるよう分離している。
            "cargo-clippy-args": ["clippy", "--all-targets"],
            "cargo-clippy-lint-args": ["--", "-D", "warnings"],
            "cargo-clippy-fix-args": ["--fix", "--allow-staged", "--allow-dirty", "--", "-D", "warnings"],
            "cargo-clippy-pass-filenames": False,
            "cargo-clippy-fast": True,
        },
    ),
    "cargo-check": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_cargo_check"),
        structured_output=(
            "cargo-check-structured",
            CARGO_SHORT_OUTPUT,
        ),
        type="linter",
        targets=["*.rs", "Cargo.toml"],
        serial_group="cargo",
        fixed_cost=2.0,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            "cargo-check-structured": True,
            "cargo-check": False,
            "cargo-check-path": "",
            "cargo-check-runner": "bin-runner",
            "cargo-check-version": "latest",
            "cargo-check-args": ["check", "--all-targets"],
            "cargo-check-pass-filenames": False,
            "cargo-check-fast": False,
        },
    ),
    "cargo-deny": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_cargo_deny"),
        structured_output=(
            "cargo-deny-structured",
            StructuredOutputSpec(
                inject=["--format=human", "--color=never"], conflicts=["--format", "--color"], before_argument="check"
            ),
        ),
        type="linter",
        targets=["Cargo.toml", "Cargo.lock", "deny.toml"],
        serial_group="cargo",
        fixed_cost=1.0,
        bin_spec=BinToolSpec(bin_name="cargo-deny", mise_backend="aqua:EmbarkStudios/cargo-deny"),
        language="rust",
        defaults={
            "cargo-deny-structured": True,
            "cargo-deny": False,
            "cargo-deny-path": "",
            "cargo-deny-runner": "bin-runner",
            "cargo-deny-version": "latest",
            "cargo-deny-args": ["check"],
            "cargo-deny-pass-filenames": False,
            "cargo-deny-fast": False,
        },
    ),
    "dotnet-build": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_dotnet_build"),
        type="linter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props"],
        serial_group="dotnet",
        fixed_cost=5.0,
        bin_spec=BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
        language="dotnet",
        defaults={
            "dotnet-build": False,
            "dotnet-build-path": "",
            "dotnet-build-runner": "bin-runner",
            "dotnet-build-version": "latest",
            "dotnet-build-args": ["build", "--nologo"],
            "dotnet-build-pass-filenames": False,
            "dotnet-build-fast": False,
        },
    ),
}
