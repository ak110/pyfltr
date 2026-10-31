import contextlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
import typing

import anyio
import mcp
import mcp_types
import psutil
import pytest
import tomlkit

import pyfltr.cli.mcp_server
from tests.conftest import write_slot_wait_worker


class _Client:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.messages: queue.Queue[dict[str, typing.Any]] = queue.Queue()
        self.pending: dict[int, dict[str, typing.Any]] = {}
        self.errors: list[Exception] = []
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
        except (OSError, ValueError) as exc:
            self.errors.append(exc)

    def send(self, method: str, params: dict[str, typing.Any], identifier: int | None = None) -> None:
        message: dict[str, typing.Any] = {"jsonrpc": "2.0", "method": method, "params": params}
        if identifier is not None:
            message["id"] = identifier
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def receive(self, identifier: int) -> dict[str, typing.Any]:
        deadline = time.monotonic() + 15
        while identifier not in self.pending:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if "id" in message:
                self.pending[message["id"]] = message
            if time.monotonic() >= deadline and identifier not in self.pending:
                raise TimeoutError(identifier)
        return self.pending.pop(identifier)


@contextlib.contextmanager
def _client(root: pathlib.Path, server_argv: list[str]) -> typing.Generator[_Client, None, None]:
    env = dict(os.environ)
    env["PYFLTR_CACHE_DIR"] = str(root / "cache")
    env["PYFLTR_GLOBAL_CONFIG"] = str(root / "global.toml")
    with (
        (root / "server-stderr.log").open("w", encoding="utf-8") as stderr,
        subprocess.Popen(
            server_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr,
            cwd=root,
            env=env,
            text=True,
            encoding="utf-8",
        ) as process,
    ):
        client = _Client(process)
        try:
            client.send(
                "initialize",
                {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                1,
            )
            assert "result" in client.receive(1)
            client.send("notifications/initialized", {})
            yield client
        finally:
            # 失敗時もテストが所有する入力だけを解放し、サーバーの終了処理を待つ。
            for work_dir in root.glob("project-*"):
                (work_dir / "release").touch()
            assert process.stdin is not None
            process.stdin.close()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
            client.reader.join(timeout=5)
        assert not client.reader.is_alive()
        assert not client.errors


_BLOCKING_SCRIPT = """import json
import os
import pathlib
import subprocess
import sys
import time

root = pathlib.Path(sys.argv[1])


def wait_release():
    deadline = time.monotonic() + 30
    while not (root / "release").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("release")
        time.sleep(0.01)


if len(sys.argv) == 2:
    with subprocess.Popen([sys.executable, __file__, str(root), "child"], start_new_session=os.name != "nt") as child:
        (root / "started.json").write_text(json.dumps({"parent": os.getpid(), "child": child.pid}), encoding="utf-8")
        try:
            wait_release()
        finally:
            # 解放待ちが失敗しても子を残さない。子は同じ解放待ちで終わる。
            if child.poll() is None:
                child.kill()
    (root / "finished").touch()
else:
    (root / "child-started").touch()
    wait_release()
"""


def _project(root: pathlib.Path, name: str) -> pathlib.Path:
    project = root / name
    project.mkdir()
    helper = project / "blocking.py"
    helper.write_text(_BLOCKING_SCRIPT, encoding="utf-8")
    after = project / "after.py"
    after.write_text("import pathlib\npathlib.Path('after-started').touch()\n", encoding="utf-8")
    (project / "sample.txt").write_text("value\n", encoding="utf-8")
    config = {
        "tool": {
            "pyfltr": {
                "respect-gitignore": False,
                "custom-commands": {
                    "slow-format": {
                        "type": "formatter",
                        "path": sys.executable,
                        "args": [str(helper), str(project)],
                        "targets": "*.txt",
                        "pass-filenames": False,
                    },
                    "after-check": {
                        "type": "linter",
                        "path": sys.executable,
                        "args": [str(after)],
                        "targets": "*.txt",
                        "pass-filenames": False,
                    },
                },
            }
        },
    }
    (project / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    return project


_SLOT_WAIT_SERVER = """import sys

import anyio

import pyfltr.cli.main

_open_process = anyio.open_process


async def _open(command, **kwargs):
    # 公開のworker起動引数のうち処理名だけを引き継ぎ、待機の観測を加えたworkerを起動する。
    return await _open_process([command[0], "-I", {worker!r}, command[-1]], **kwargs)


anyio.open_process = _open
sys.argv = [sys.argv[0], "mcp"]
pyfltr.cli.main.main()
"""


def _slot_wait_server(root: pathlib.Path, marker: pathlib.Path) -> list[str]:
    """workerが実行枠の待機を始めた時点で`marker`を作成するstdioサーバーの起動引数を返す。"""
    worker = write_slot_wait_worker(root / "slot_wait_worker.py", marker)
    server = root / "slot_wait_server.py"
    server.write_text(_SLOT_WAIT_SERVER.format(worker=str(worker)), encoding="utf-8")
    return [sys.executable, str(server)]


def _wait_for(predicate: typing.Callable[[], bool], observed: typing.Callable[[], str] | None = None) -> None:
    """条件が成立するまで待つ。上限に達したら、`observed`の戻り値（失敗時点の観測）を示して失敗させる。"""
    deadline = time.monotonic() + 15
    while not predicate():
        if time.monotonic() >= deadline:
            detail = f"観測した状態:\n{observed()}" if observed is not None else ""
            raise TimeoutError(f"観測条件へ到達しませんでした。{detail}")
        time.sleep(0.01)


def _describe_slot_wait(root: pathlib.Path, waiting: pathlib.Path, client: _Client) -> str:
    """待機の印に到達しない原因を調べるため、印の有無・サーバーとworkerの標準エラー・未回収の応答を返す。"""
    stderr = root / "server-stderr.log"
    stderr_text = stderr.read_text(encoding="utf-8") if stderr.exists() else "（未作成）"
    return "\n".join(
        [
            f"待機の印 {waiting}: {'あり' if waiting.exists() else 'なし'}",
            f"サーバーとworkerの標準エラー {stderr}:\n{stderr_text}",
            f"未回収の応答: {client.pending!r}",
            f"サーバーの終了コード: {client.process.poll()}",
        ]
    )


def _running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_stdio_remains_responsive_and_cancels_only_owned_process_tree(tmp_path: pathlib.Path) -> None:
    """2つの実行を重ね、pingと警告応答、片方の子孫停止、もう片方の完走を確認する。

    2つの`run`は検証の実行枠を共有するため、後発の要求は先発の取り消しで子孫が止まり枠が解放されてから実行する。
    """
    first = _project(tmp_path, "project-first")
    second = _project(tmp_path, "project-second")
    waiting = tmp_path / "slot-waiting"

    def _run_request(identifier: int, project: pathlib.Path) -> None:
        client.send(
            "tools/call",
            {
                "name": "run",
                "arguments": {
                    "paths": ["sample.txt"],
                    "commands": ["slow-format", "after-check"],
                    "work_dir": str(project),
                    "no_cache": True,
                },
            },
            identifier,
        )

    with _client(tmp_path, _slot_wait_server(tmp_path, waiting)) as client:
        _run_request(2, first)
        _wait_for(lambda: (first / "child-started").exists() and (first / "started.json").exists())
        first_pids = json.loads((first / "started.json").read_text(encoding="utf-8"))
        _run_request(3, second)
        client.send("ping", {}, 4)
        assert "result" in client.receive(4)
        assert not (first / "release").exists() and not (second / "release").exists()
        client.send(
            "tools/call",
            {
                "name": "grep",
                "arguments": {
                    "paths": [str(tmp_path / "missing.txt")],
                    "pattern": "value",
                    "no_exclude": True,
                    "no_gitignore": True,
                },
            },
            5,
        )
        grep = client.receive(5)["result"]["structuredContent"]
        assert grep["warnings"]
        assert not (second / "started.json").exists()
        # 後発が実行枠の待機を始めたことを確かめてから先発を取り消し、待機の警告を検証できる順序にする。
        _wait_for(waiting.exists, observed=lambda: _describe_slot_wait(tmp_path, waiting, client))
        client.send("notifications/cancelled", {"requestId": 2})
        _wait_for(lambda: (second / "child-started").exists() and (second / "started.json").exists())
        assert not any(_running(pid) for pid in first_pids.values())
        second_pids = json.loads((second / "started.json").read_text(encoding="utf-8"))
        assert all(_running(pid) for pid in second_pids.values())
        assert not (first / "finished").exists()
        assert not (first / "after-started").exists()
        (second / "release").touch()
        completed = client.receive(3)["result"]["structuredContent"]
        assert completed["exit_code"] == 0
        assert [warning["source"] for warning in completed["warnings"]] == ["execution-slot"]
        assert (second / "after-started").exists()
        client.send("tools/call", {"name": "list_runs", "arguments": {}}, 6)
        assert "result" in client.receive(6)
        assert 2 not in client.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["cp1252", "cp932"])
async def test_mcp_worker_uses_utf8_with_non_utf8_standard_streams(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    """標準入出力が非UTF-8でも、公開MCPの日本語入力・結果・エラーを失わない。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path / "cache"))
    sample = tmp_path / "sample.txt"
    sample.write_text("日本語\n", encoding="utf-8")
    wrapper = tmp_path / "worker_streams.py"
    wrapper.write_text(
        "import sys\nimport pyfltr.cli.mcp_worker\n"
        f"sys.stdin.reconfigure(encoding={encoding!r})\n"
        f"sys.stdout.reconfigure(encoding={encoding!r})\n"
        "sys.exit(pyfltr.cli.mcp_worker.main())\n",
        encoding="utf-8",
    )
    open_process = anyio.open_process

    async def _open(command: list[str], **kwargs: typing.Any) -> anyio.abc.Process:
        # OSの標準ストリームを差し替えた実インタプリタで、同じworker入口を実行する。
        return await open_process([command[0], "-I", str(wrapper), command[-1]], **kwargs)

    monkeypatch.setattr(anyio, "open_process", _open)
    async with mcp.Client(pyfltr.cli.mcp_server.build_server()) as client:
        result = await client.call_tool(
            "grep",
            {"paths": [str(sample)], "pattern": "日本語", "no_exclude": True, "no_gitignore": True},
        )
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["matches"][0]["match_text"] == "日本語"
        failure = await client.call_tool("show_run_output", {"run_id": "nonexistent", "commands": ["ruff-check"]})
        assert failure.is_error
        assert any(
            "run_id が見つかりません" in content.text
            for content in failure.content
            if isinstance(content, mcp_types.TextContent)
        )
