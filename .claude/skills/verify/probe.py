"""隔離したプロジェクトでCLI・MCPを実プロセスとして操作する。"""

import argparse
import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import typing

import tomlkit

import pyfltr


def cli(project: pathlib.Path, evidence: pathlib.Path, env: dict[str, str], *args: str) -> None:
    """CLIの出力と終了コードを保存する。"""
    result = subprocess.run(
        [sys.executable, "-m", "pyfltr", *args],
        cwd=project,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    (evidence / f"{args[0]}.json").write_text(
        json.dumps({"args": args, "exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr}),
        encoding="utf-8",
    )
    result.check_returncode()


def mcp(project: pathlib.Path, evidence: pathlib.Path, env: dict[str, str], tool: str) -> None:
    """stdioの初期化と1件のツール呼出しを記録する。"""
    messages: queue.Queue[str | None] = queue.Queue()
    with (
        (evidence / "rpc.jsonl").open("w", encoding="utf-8") as transcript,
        (evidence / "server-stderr.log").open("w", encoding="utf-8") as stderr,
        subprocess.Popen(
            [sys.executable, "-m", "pyfltr", "mcp"],
            cwd=project,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
        ) as process,
    ):
        stdin, stdout = process.stdin, process.stdout
        if stdin is None or stdout is None:
            raise RuntimeError("MCP pipes unavailable")

        def read() -> None:
            for line in stdout:
                messages.put(line)
            messages.put(None)

        def log(direction: str, message: object) -> None:
            print(json.dumps({direction: message}, ensure_ascii=False), file=transcript, flush=True)

        def send(method: str, params: dict[str, object], identifier: int | None = None) -> None:
            message: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": params}
            if identifier is not None:
                message["id"] = identifier
            log("send", message)
            stdin.write(json.dumps(message) + "\n")
            stdin.flush()

        def receive(identifier: int) -> dict[str, typing.Any]:
            for _ in range(30):
                line = messages.get(timeout=15)
                if line is None:
                    raise RuntimeError("MCP stdout closed")
                message = json.loads(line)
                log("receive", message)
                if message.get("id") == identifier:
                    return message["result"]
            raise RuntimeError("MCP response ID not received")

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            send(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "verify", "version": "1"},
                },
                1,
            )
            if "tools" not in receive(1)["capabilities"]:
                raise RuntimeError("MCP tools unavailable")
            send("notifications/initialized", {})
            args: dict[str, object] = (
                {"paths": ["sample.txt"], "commands": ["probe"], "work_dir": str(project), "no_cache": True}
                if tool == "run"
                else {"paths": [str(project / "sample.txt")], "pattern": "before", "replacement": "after", "show_changes": True}
            )
            send("tools/call", {"name": tool, "arguments": args}, 2)
            response = receive(2)
            content = response.get("structuredContent", {})
            if response.get("isError") or (
                content.get("exit_code") != 0 if tool == "run" else "after" not in json.dumps(content)
            ):
                raise RuntimeError(response)
        finally:
            stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
            reader.join(timeout=5)
            log("exit", process.returncode)
        if process.returncode or reader.is_alive():
            raise RuntimeError(f"MCP shutdown: {process.returncode}")


def exercise(project: pathlib.Path, evidence: pathlib.Path, env: dict[str, str], operation: str) -> None:
    """入力を作り、操作結果をファイルの状態と照合する。"""
    sample = project / "sample.txt"
    sample.write_text("before\n", encoding="utf-8")
    (project / "check.py").write_text(
        "from pathlib import Path\nPath('checked').write_text(Path('sample.txt').read_text())\n",
        encoding="utf-8",
    )
    config = {
        "tool": {
            "pyfltr": {
                "custom-commands": {
                    "probe": {
                        "type": "linter",
                        "path": sys.executable,
                        "args": ["check.py"],
                        "targets": "*.txt",
                        "fast": True,
                        "pass-filenames": False,
                    }
                }
            }
        }
    }
    (project / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    expected = "before\n"
    observed = sample
    if operation in {"run", "ci", "fast"}:
        cli(project, evidence, env, operation, "sample.txt", "--commands=probe", "--no-cache", "--output-format=jsonl")
        observed = project / "checked"
    elif operation == "grep":
        cli(project, evidence, env, "grep", "before", "sample.txt", "--output-format=jsonl")
        if "before" not in json.loads((evidence / "grep.json").read_text(encoding="utf-8"))["stdout"]:
            raise RuntimeError("grep match missing")
    elif operation == "replace":
        cli(project, evidence, env, "replace", "before", "after", "sample.txt", "--output-format=jsonl")
        expected = "after\n"
    else:
        tool = operation.removeprefix("mcp-")
        mcp(project, evidence, env, tool)
        if tool == "run":
            observed = project / "checked"
    actual = observed.read_text(encoding="utf-8")
    (evidence / "observed.txt").write_text(actual, encoding="utf-8")
    if actual != expected:
        raise RuntimeError(f"{observed.name}: {actual!r} != {expected!r}")


def main() -> None:
    """新規の証拠ディレクトリを確保し、一時プロジェクトを後始末する。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["run", "ci", "fast", "grep", "replace", "mcp-run", "mcp-replace"])
    parser.add_argument("evidence", type=pathlib.Path)
    args = parser.parse_args()
    evidence = args.evidence.resolve()
    repo = pathlib.Path(__file__).resolve().parents[3]
    module = pathlib.Path(pyfltr.__file__).resolve()
    if module != repo / "pyfltr" / "__init__.py":
        raise RuntimeError(f"Wrong pyfltr: {module}")
    evidence.mkdir()
    with tempfile.TemporaryDirectory(dir=evidence.parent, prefix="pyfltr-verify-") as directory:
        project = pathlib.Path(directory)
        env = {
            **os.environ,
            "PYFLTR_CACHE_DIR": str(project / "cache"),
            "PYFLTR_GLOBAL_CONFIG": str(project / "global.toml"),
            "PYTHONPATH": str(repo),
            "PYTHONIOENCODING": "utf-8",
        }
        (project / "global.toml").touch()
        state = {key: env[key] for key in ("PYFLTR_CACHE_DIR", "PYFLTR_GLOBAL_CONFIG", "PYTHONPATH")}
        (evidence / "state.json").write_text(json.dumps({"module": str(module), **state}), encoding="utf-8")
        cli(project, evidence, env, "--version")
        exercise(project, evidence, env, args.operation)
    if project.exists():
        raise RuntimeError(f"Cleanup failed: {project}")
    (evidence / "cleanup.txt").write_text(f"removed: {project}\n", encoding="utf-8")
    print(evidence)


if __name__ == "__main__":
    main()
