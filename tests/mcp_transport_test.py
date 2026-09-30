"""公開stdioで、処理中の受信・キャンセル・要求間の状態分離を検証する。"""

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

import psutil
import tomlkit


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
def _client(root: pathlib.Path) -> typing.Iterator[_Client]:
    env = dict(os.environ)
    env["PYFLTR_CACHE_DIR"] = str(root / "cache")
    env["PYFLTR_GLOBAL_CONFIG"] = str(root / "global.toml")
    with (
        (root / "server-stderr.log").open("w", encoding="utf-8") as stderr,
        subprocess.Popen(
            [sys.executable, "-m", "pyfltr", "mcp"],
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
if len(sys.argv) == 2:
    child = subprocess.Popen([sys.executable, __file__, str(root), "child"], start_new_session=os.name != "nt")
    (root / "started.json").write_text(json.dumps({"parent": os.getpid(), "child": child.pid}), encoding="utf-8")
else:
    (root / "child-started").touch()
deadline = time.monotonic() + 30
while not (root / "release").exists():
    if time.monotonic() >= deadline:
        raise TimeoutError("release")
    time.sleep(0.01)
if len(sys.argv) == 2:
    child.wait(timeout=5)
    (root / "finished").touch()
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


def _wait_for(predicate: typing.Callable[[], bool]) -> None:
    deadline = time.monotonic() + 15
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("観測条件へ到達しませんでした")
        time.sleep(0.01)


def _running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_stdio_remains_responsive_and_cancels_only_owned_process_tree(tmp_path: pathlib.Path) -> None:
    """2つの実行を重ね、pingと警告応答、片方の子孫停止、もう片方の完走を確認する。"""
    first = _project(tmp_path, "project-first")
    second = _project(tmp_path, "project-second")
    with _client(tmp_path) as client:
        for identifier, project in ((2, first), (3, second)):
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
        _wait_for(
            lambda: all(
                (project / "child-started").exists() and (project / "started.json").exists() for project in (first, second)
            )
        )
        first_pids = json.loads((first / "started.json").read_text(encoding="utf-8"))
        second_pids = json.loads((second / "started.json").read_text(encoding="utf-8"))
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
        client.send("notifications/cancelled", {"requestId": 2})
        _wait_for(lambda: not any(_running(pid) for pid in first_pids.values()))
        assert all(_running(pid) for pid in second_pids.values())
        assert not (first / "finished").exists()
        assert not (first / "after-started").exists()
        (second / "release").touch()
        completed = client.receive(3)["result"]["structuredContent"]
        assert completed["exit_code"] == 0
        assert not completed["warnings"]
        assert (second / "after-started").exists()
        client.send("tools/call", {"name": "list_runs", "arguments": {}}, 6)
        assert "result" in client.receive(6)
        assert 2 not in client.pending
