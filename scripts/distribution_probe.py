"""wheel導入先のPythonだけでCLI・辞書・MCPの公開入口を検査する。"""

import asyncio
import contextlib
import importlib.metadata
import json
import os
import pathlib
import subprocess
import sys

import mcp
import mcp.client.stdio

import pyfltr


def _cli(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    command = pathlib.Path(sys.executable).parent / ("pyfltr.exe" if os.name == "nt" else "pyfltr")
    return subprocess.run(
        [str(command), *arguments],
        cwd=pathlib.Path.cwd(),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=60,
    )


async def _mcp() -> None:
    params = mcp.client.stdio.StdioServerParameters(
        command=sys.executable, args=["-I", "-m", "pyfltr", "mcp"], cwd=pathlib.Path.cwd(), env=dict(os.environ)
    )
    async with contextlib.AsyncExitStack() as stack:
        read_stream, write_stream = await stack.enter_async_context(mcp.client.stdio.stdio_client(params))
        session = await stack.enter_async_context(mcp.ClientSession(read_stream, write_stream))
        await session.initialize()
        tools = await session.list_tools()
        assert {"run", "grep", "replace", "replace_undo"}.issubset(tool.name for tool in tools.tools)
        result = await session.call_tool(
            "run",
            {
                "paths": ["sample.py"],
                "commands": ["ruff-check"],
                "work_dir": str(pathlib.Path.cwd()),
                "no_fix": True,
                "no_cache": True,
            },
        )
        assert not result.is_error, result
        assert result.structured_content is not None
        assert result.structured_content["exit_code"] == 0
        assert result.structured_content["run_id"]


def main() -> int:
    """開発ソースを参照せず、配布された入口とデータで結果へ到達する。"""
    assert pyfltr.__file__ is not None
    assert pathlib.Path(pyfltr.__file__).resolve().is_relative_to(pathlib.Path(sys.prefix).resolve())
    assert importlib.metadata.version("pyfltr") == sys.argv[1]
    help_result = _cli(["--help"])
    assert help_result.returncode == 0, help_result.stderr
    version_result = _cli(["--version"])
    assert version_result.returncode == 0 and sys.argv[1] in version_result.stdout + version_result.stderr, version_result
    root = pathlib.Path.cwd()
    (root / "pyproject.toml").write_text(
        '[tool.pyfltr]\nruff-check = true\npython-runner = "direct"\n'
        'colloquial-check = true\ncolloquial-check-severity = "error"\nrespect-gitignore = false\n',
        encoding="utf-8",
    )
    (root / "sample.py").write_text("value = 1\n", encoding="utf-8")
    # 辞書の同梱だけでなく、配布CLIから検出結果まで到達することを確認する。
    (root / "sample.md").write_text("\u30ac\u30c1\n", encoding="utf-8")
    result = _cli(
        [
            "run",
            "sample.py",
            "sample.md",
            "--commands=ruff-check,colloquial-check",
            "--no-fix",
            "--no-cache",
            "--no-archive",
            "--no-clear",
            "--output-format=jsonl",
        ]
    )
    assert result.returncode == 1, result.stderr
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    statuses = {record["command"]: record["status"] for record in records if record["kind"] == "command"}
    assert statuses["ruff-check"] == "succeeded", records
    assert statuses["colloquial-check"] == "failed", records
    assert any(record["kind"] == "diagnostic" and record["command"] == "colloquial-check" for record in records)
    assert records[-1]["exit"] == result.returncode
    asyncio.run(_mcp())
    print("CLI・内蔵辞書・MCP・linterの公開入口を確認しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
