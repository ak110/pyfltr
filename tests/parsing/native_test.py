"""実ツールで採取したコンパイル・テスト・検査失敗の位置を検証する。"""

import json
import pathlib

import pytest

import pyfltr.parsing.entry

OUTPUTS = json.loads(pathlib.Path(__file__).with_name("data").joinpath("registered_outputs.json").read_text(encoding="utf-8"))
EXPECTED = {
    "hadolint": ("Dockerfile", 1),
    "gitleaks": ("sample.txt", 1),
    "tsc": ("sample.ts", 1),
    "oxlint": ("sample.js", 1),
    "cargo-clippy": ("src/main.rs", 3),
    "cargo-check": ("src/main.rs", 2),
    "cargo-test": ("src/lib.rs", 5),
    "cargo-deny": ("Cargo.toml", 5),
    "dotnet-build": ("Program.cs", 1),
    "dotnet-test": ("SampleTest.cs", 4),
}


@pytest.mark.parametrize("command", EXPECTED)
def test_native_real_output_has_position(command: str) -> None:
    """位置付き失敗はファイル・行・本文を持つ診断になる。"""
    errors = pyfltr.parsing.entry.parse_errors(command, OUTPUTS[command]["output"])
    assert errors
    assert (errors[0].file, errors[0].line) == EXPECTED[command]
    assert all(error.message for error in errors)


@pytest.mark.parametrize("command", EXPECTED)
def test_native_failure_without_position_has_no_diagnostic(command: str) -> None:
    """位置の無い環境エラーからファイルと行を捏造しない。"""
    assert not pyfltr.parsing.entry.parse_errors(command, "error: test host failed to start\n")


def test_cargo_deny_synthetic_manifest_is_not_a_file() -> None:
    """合成manifestのURIを診断のファイルへ混入させない。"""
    output = "error[unlicensed]: license not found\n  ┌─ crate://sample:1:1\n"
    assert not pyfltr.parsing.entry.parse_errors("cargo-deny", output)


def test_gitleaks_diagnostic_does_not_include_match_or_secret() -> None:
    """伏せ字レポートへ依存せず、診断本文にも秘密値を転記しない。"""
    finding = {
        "File": "sample.txt",
        "StartLine": 1,
        "RuleID": "sample",
        "Match": "fictional-match",
        "Secret": "fictional-secret",
    }
    errors = pyfltr.parsing.entry.parse_errors("gitleaks", json.dumps([finding]))
    assert len(errors) == 1
    assert "fictional" not in errors[0].message


@pytest.mark.parametrize("severity", ["warning", "error"])
def test_cargo_human_lint_rule(severity: str) -> None:
    """構造化出力を無効にしたClippyの位置と注記のlint名を保つ。"""
    # cargo-clippy-structured=falseの実出力と同じ注記形式。重要度の両経路を検証する。
    level = "warn" if severity == "warning" else "deny"
    output = (
        f"{severity}: equal expressions as operands to `==`\n --> src/main.rs:3:13\n"
        "  |\n3 |     let _ = value == value;\n  |             ^^^^^^^^^^^^^^\n"
        f"  = note: `#[{level}(clippy::eq_op)]` on by default\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("cargo-clippy", output)
    assert len(errors) == 1
    assert (errors[0].file, errors[0].line, errors[0].col, errors[0].rule, errors[0].severity) == (
        "src/main.rs",
        3,
        13,
        "clippy::eq_op",
        severity,
    )


def test_cargo_short_warning_rule() -> None:
    """短い形式のlint名付き警告もruleへ格納する。"""
    errors = pyfltr.parsing.entry.parse_errors("cargo-clippy", "src/main.rs:3:13: warning[clippy::eq_op]: equal expressions\n")
    assert len(errors) == 1
    assert (errors[0].rule, errors[0].severity) == ("clippy::eq_op", "warning")


def test_dotnet_build_range_location() -> None:
    """MSBuildの公式な4座標形式で開始位置と本文を保つ。"""
    output = "bad path:sample.cs(2,3,4,5): error CS1002: expected token [sample.csproj]\n"
    errors = pyfltr.parsing.entry.parse_errors("dotnet-build", output)
    assert len(errors) == 1
    assert (errors[0].file, errors[0].line, errors[0].col, errors[0].message) == ("bad path:sample.cs", 2, 3, "expected token")
    assert (errors[0].end_line, errors[0].end_col) == (4, 5)


def test_dotnet_test_japanese_message() -> None:
    """日本語ロケールのVSTestで実測した見出しから本文を取り出す。"""
    output = (
        "  エラー メッセージ:\n   Assert.Equal() Failure: Values differ\nExpected: 1\nActual:   2\n"
        "  スタック トレース:\n     at SampleTest.Fails() in SampleTest.cs:line 4\n"
    )
    errors = pyfltr.parsing.entry.parse_errors("dotnet-test", output)
    assert len(errors) == 1
    assert (errors[0].file, errors[0].line, errors[0].message) == ("SampleTest.cs", 4, "Assert.Equal() Failure: Values differ")
