"""組み込みパーサーの登録・特殊パス・親子の帰属とCLI・MCP診断を検査する。"""

import asyncio
import json
import pathlib
import subprocess
import sys

import pytest

import pyfltr.cli.mcp_server
import pyfltr.parsing.entry
import pyfltr.tools
from tests.parsing.native_test import EXPECTED, OUTPUTS

_FIXTURES = json.loads(
    pathlib.Path(__file__).parents[1].joinpath("parsing/data/registered_outputs.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    "command", [name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.type in ("linter", "tester")]
)
def test_builtin_linters_and_testers_have_parser_and_failure_sample(command: str) -> None:
    """全定義へ解析手段と失敗検体を要求し、診断の欠落を登録時に検出する。"""
    info = pyfltr.tools.BUILTIN_COMMANDS[command]
    assert info.error_pattern or info.diagnostic_pattern or info.parser or info.context_parser or info.path_resolving_parser, (
        f"{command}: tools.pyへ解析手段を登録し、registered_outputs.jsonへ位置付き失敗の検体を追加してください"
    )
    assert command in _FIXTURES, f"{command}: registered_outputs.jsonへ実出力に基づく失敗検体を追加してください"
    fixture = _FIXTURES[command]
    errors = pyfltr.parsing.entry.parse_errors(command, fixture["output"], error_pattern=info.error_pattern)
    if fixture["reports_line"]:
        assert any(error.file and error.line > 0 for error in errors), (
            f"{command}: 位置付き失敗の検体から診断を得られません。解析手段と検体を修正してください"
        )
    else:
        # 行を報告しないツールも、実出力の構造と理由を検査してファイル診断を保持する。
        finding = json.loads(fixture["output"])["findings"]
        assert fixture["position_reason"]
        assert finding and all("line" not in item and "location" not in item for item in finding)
        assert errors and all(error.file and error.line == 0 for error in errors)


@pytest.mark.parametrize("command", EXPECTED)
def test_native_output_reaches_jsonl_and_mcp(command: str, tmp_path: pathlib.Path) -> None:
    """実行ファイルだけをスタブへ差し替え、登録・解析・保存・返却の経路を通す。"""
    filename, expected_line = EXPECTED[command]
    target = tmp_path / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("sample\n", encoding="utf-8")
    output = OUTPUTS[command]["output"]
    script = f"import sys; sys.stdout.write({output!r}); sys.exit(1)"
    config = (
        f"[tool.pyfltr]\n{command} = true\nrespect-gitignore = false\n"
        f"{command}-path = {json.dumps(sys.executable)}\n{command}-args = {json.dumps(['-c', script])}\n"
        f"{command}-targets = ['*']\n"
    )
    (tmp_path / "pyproject.toml").write_text(config, encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pyfltr",
            "run",
            "--work-dir",
            str(tmp_path),
            f"--commands={command}",
            "--no-fix",
            "--no-cache",
            "--output-format=jsonl",
            filename,
        ],
        capture_output=True,
        encoding="utf-8",
        errors="backslashreplace",
        check=False,
        timeout=45,
        cwd=tmp_path,
    )
    assert proc.returncode == 1, proc.stderr + proc.stdout
    records = [json.loads(line) for line in proc.stdout.splitlines() if line]
    diagnostic = next(item for item in records if item.get("kind") == "diagnostic")
    assert diagnostic["file"] == filename
    assert diagnostic["messages"][0]["line"] == expected_line
    run_id = next(item["run_id"] for item in records if item.get("kind") == "header")
    result = asyncio.run(pyfltr.cli.mcp_server.tool_show_run_diagnostics(run_id, [command]))
    assert result[0].diagnostics[0].file == filename
    assert result[0].diagnostics[0].messages[0].line == expected_line
    if command == "gitleaks":
        assert '"Secret": "REDACTED"' in output
