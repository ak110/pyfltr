"""協調所有者・pyfltrのCLI・試験用ツールの3段で、参加券による多段の協調譲渡を確かめる結合試験。

最上位の協調所有者のプロセスが参加券を環境変数でpyfltrのCLIへ渡し、
CLIは起動するツールへツール1件専用の参加券を同じ環境変数で渡す。
ツールは公開API（`receive_ticket`・`hold(participant=...)`・`yield_to_waiters`）だけで区切りを通知する。
保存先は`tests/conftest.py`の既定隔離が固定した`PYFLTR_CACHE_DIR`を子プロセスへ継承し、全段が同じ実行枠を使う。
"""

import contextlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import typing

import psutil
import pytest
import tomlkit

import pyfltr.command.execution_slot as slot

_TOOL_SCRIPT = """import json
import os
import pathlib
import subprocess
import sys
import time

import pyfltr.command.execution_slot as slot

root = pathlib.Path(sys.argv[1])


def wait_for(name):
    deadline = time.monotonic() + 60
    while not (root / name).exists():
        if time.monotonic() > deadline:
            raise TimeoutError(name)
        time.sleep(0.01)


ticket = slot.receive_ticket()
(root / "tool-ticket.json").write_text(json.dumps(ticket), encoding="utf-8")
with slot.hold(participant=ticket):
    (root / "tool-step1").touch()
    if (root / "dup").exists():
        # 参加中のプロセスの子孫が同じ参加券で参加しようとする事故を再現する。
        child = subprocess.run([sys.executable, str(root / "dup.py"), ticket], check=False)
        (root / "dup-result").write_text(str(child.returncode), encoding="utf-8")
    wait_for("boundary")
    if (root / "leaf-crash").exists():
        # 参加中に異常終了し、停止を通知しないまま参加の登録を残す。
        os._exit(3)
    slot.yield_to_waiters()
    (root / "tool-step2").touch()
print("tool output", flush=True)
"""

_DUP_SCRIPT = """import sys

import pyfltr.command.execution_slot as slot

try:
    with slot.hold(participant=sys.argv[1]):
        pass
except slot.WaitAborted:
    sys.exit(9)
sys.exit(0)
"""

_PLAIN_SCRIPT = """import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
(root / "plain-active").touch()
deadline = time.monotonic() + 60
while not (root / "plain-release").exists():
    if time.monotonic() > deadline:
        raise TimeoutError("plain-release")
    time.sleep(0.01)
(root / "plain-done").touch()
"""


def _wait_for(path: pathlib.Path, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(0.01)


def _make_project(root: pathlib.Path, *, plain: bool) -> pathlib.Path:
    """試験用ツール（と参加しないツール）をカスタムコマンドに持つプロジェクトを作成する。"""
    project = root / "project"
    project.mkdir()
    (project / "sample.txt").write_text("value\n", encoding="utf-8")
    tool = root / "tool.py"
    tool.write_text(_TOOL_SCRIPT, encoding="utf-8")
    (root / "dup.py").write_text(_DUP_SCRIPT, encoding="utf-8")
    (root / "plain.py").write_text(_PLAIN_SCRIPT, encoding="utf-8")
    commands = {
        "step-tool": {
            "type": "tester",
            "path": sys.executable,
            "args": [str(tool), str(root)],
            "targets": "*.txt",
            "pass-filenames": False,
        }
    }
    if plain:
        commands["plain-tool"] = {
            "type": "tester",
            "path": sys.executable,
            "args": [str(root / "plain.py"), str(root)],
            "targets": "*.txt",
            "pass-filenames": False,
        }
    config = {"tool": {"pyfltr": {"respect-gitignore": False, "jobs": 2, "custom-commands": commands}}}
    (project / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    (root / "commands").write_text(",".join(commands), encoding="utf-8")
    return project


def _start(root: pathlib.Path, role: str, stack: contextlib.ExitStack) -> subprocess.Popen[str]:
    """役割のプロセスを起動する。出力は試験の一時領域へ保存し、失敗時の診断に使う。"""
    output = stack.enter_context((root / f"{role}.log").open("w", encoding="utf-8"))
    return stack.enter_context(
        subprocess.Popen(
            [sys.executable, str(pathlib.Path(__file__).resolve()), role, str(root)],
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "normal",
        "plain-running",
        "dup",
        "leaf-crash",
        "owner-abort",
        pytest.param(
            "middle-crash",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                # Windowsの仮想環境のpython.exeは本体を子プロセスとして起動する中継役で、
                # 起動したプロセスの強制終了では中間段のpyfltr本体を止められないため
                reason="中間段の強制終了をPOSIXのプロセスで再現する",
            ),
        ),
    ],
)
def test_three_level_cooperation(tmp_path: pathlib.Path, scenario: str) -> None:
    """ツールの区切りで独立した待機者が実行され、その後に残りが再開する。"""
    plain = scenario == "plain-running"
    _make_project(tmp_path, plain=plain)
    for name in ("dup", "leaf-crash"):
        if scenario == name:
            (tmp_path / name).touch()
    stack = contextlib.ExitStack()
    owner = _start(tmp_path, "owner", stack)
    waiter: subprocess.Popen[str] | None = None
    try:
        _wait_for(tmp_path / "tool-step1")
        if plain:
            _wait_for(tmp_path / "plain-active")
        waiter = _start(tmp_path, "waiter", stack)
        _wait_for(tmp_path / "waiting")
        # ツールの稼働中は待機者を実行しない。
        time.sleep(1.5)
        assert not (tmp_path / "waiter-ran").exists()
        if scenario == "middle-crash":
            # ツールが停止した後に中間段（pyfltrのCLI）を強制終了する。
            (tmp_path / "boundary").touch()
            _wait_for_paused_leaf(tmp_path)
            psutil.Process(int((tmp_path / "middle-pid").read_text(encoding="utf-8"))).kill()
        else:
            (tmp_path / "boundary").touch()
        if plain:
            # 参加しないツールが稼働中の間は、参加したツールが停止しても譲らない。
            time.sleep(1.5)
            assert not (tmp_path / "waiter-ran").exists()
            (tmp_path / "plain-release").touch()
        _wait_for(tmp_path / "waiter-ran")
        if scenario == "owner-abort":
            # 待機者の実行中（最上位の所有者の取り直し待機中）に取り消す。
            (tmp_path / "owner-abort").touch()
            assert owner.wait(timeout=60) == 7
        if scenario in ("normal", "plain-running", "dup"):
            # 待機者の実行中は、停止したツールの残りを再開しない。
            time.sleep(1.0)
            assert not (tmp_path / "tool-step2").exists()
        (tmp_path / "waiter-release").touch()
        assert waiter.wait(timeout=60) == 0
        owner.wait(timeout=120)
    finally:
        for name in ("boundary", "plain-release", "waiter-release"):
            (tmp_path / name).touch()
        for process in (owner, waiter):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=30)
        stack.close()
    expected_owner = 7 if scenario == "owner-abort" else 0
    assert owner.returncode == expected_owner, (tmp_path / "owner.log").read_text(encoding="utf-8")
    result = json.loads((tmp_path / "owner-result.json").read_text(encoding="utf-8"))
    if scenario == "owner-abort":
        # 協調区間が閉じられた中間段と最下段は再開せずに停止し、中間段は取消の結果を出力して終了する。
        assert not (tmp_path / "tool-step2").exists()
        assert "協調区間が閉じられました" in result["middle_output"], result["middle_output"]
        _wait_until_gone(tmp_path)
    elif scenario == "middle-crash":
        assert result["middle_returncode"] != 0
        # 協調所有者を失った最下段は、再開を待たずに停止する。
        _wait_until_gone(tmp_path)
        assert not (tmp_path / "tool-step2").exists()
    elif scenario == "leaf-crash":
        assert result["middle_returncode"] == 1
        assert not (tmp_path / "tool-step2").exists()
    else:
        assert result["middle_returncode"] == 0, result["middle_output"]
        assert (tmp_path / "tool-step2").exists()
        assert "step-tool" in result["middle_output"]
    if scenario == "dup":
        assert (tmp_path / "dup-result").read_text(encoding="utf-8") == "9"
    # 全段を回収した後は、保持者記録と生存する協調所有者の協調区間が残らない。
    # 強制終了した中間段の協調区間は閉じられずに残るが、協調所有者が終了しているため参加を受け付けない。
    assert slot.read_owner() is None
    for path in slot.default_lock_dir().glob("execution.cooperative-*.json"):
        assert scenario == "middle-crash", path
        coordinator = json.loads(path.read_text(encoding="utf-8"))["coordinator"]
        assert not psutil.pid_exists(coordinator["pid"]), path


def _wait_for_paused_leaf(root: pathlib.Path) -> None:
    """最下段が中間段の協調区間で停止を通知するまで待つ。"""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        for path in slot.default_lock_dir().glob("execution.cooperative-*.json"):
            with contextlib.suppress(OSError, ValueError):
                state = json.loads(path.read_text(encoding="utf-8"))
                coordinator = state.get("coordinator", {})
                if coordinator.get("pid") == int((root / "middle-pid").read_text(encoding="utf-8")) and any(
                    value["state"] == "paused" for value in state["participants"].values()
                ):
                    return
        time.sleep(0.01)
    raise TimeoutError("paused leaf")


def _wait_until_gone(root: pathlib.Path) -> None:
    """最下段のツールのプロセスが終了するまで待つ。"""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        alive = [
            process for process in psutil.process_iter(["cmdline"]) if str(root / "tool.py") in (process.info["cmdline"] or [])
        ]
        if not alive:
            return
        time.sleep(0.05)
    raise TimeoutError("leaf process")


def _scenario_owner(root: pathlib.Path) -> int:
    """協調所有者として参加券をpyfltrのCLIへ環境変数で渡し、CLIの稼働中に譲渡点を呼ぶ。"""
    result: dict[str, typing.Any] = {}
    try:
        _run_owner(root, result)
    except slot.WaitAborted:
        return 7
    finally:
        (root / "owner-result.json").write_text(json.dumps(result), encoding="utf-8")
    return 0


def _run_owner(root: pathlib.Path, result: dict[str, typing.Any]) -> None:
    project = root / "project"
    with (
        slot.cooperative_hold(label="owner", should_abort=(root / "owner-abort").exists) as coordinator,
        coordinator.participant() as ticket,
    ):
        env = {**os.environ, slot.TICKET_ENV: ticket}
        with subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pyfltr",
                "run",
                f"--commands={(root / 'commands').read_text(encoding='utf-8')}",
                "--output-format=text",
                "--no-ui",
                ".",
            ],
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        ) as middle:
            (root / "middle-pid").write_text(str(middle.pid), encoding="utf-8")
            output: list[str] = []
            stream = middle.stdout
            assert stream is not None
            reader = threading.Thread(target=lambda: output.extend(stream))
            reader.start()
            try:
                deadline = time.monotonic() + 120
                while middle.poll() is None:
                    coordinator.checkpoint()
                    if time.monotonic() > deadline:
                        middle.kill()
                        raise TimeoutError("middle")
                    time.sleep(0.05)
            finally:
                # 取消・例外でも中間段の終了を待ち、出力と終了状態を保持してから協調区間を閉じる。
                middle.wait(timeout=60)
                reader.join(30)
                result.update(middle_returncode=middle.returncode, middle_output="".join(output))


def _scenario_waiter(root: pathlib.Path) -> int:
    """所有者と祖先関係のない独立した待機者として同じ実行枠を待つ。"""
    with slot.hold(on_wait=lambda _: (root / "waiting").touch()):
        (root / "waiter-ran").touch()
        _wait_for(root / "waiter-release")
    return 0


def _scenario_main() -> int:
    role, directory = sys.argv[1:3]
    root = pathlib.Path(directory)
    if role == "owner":
        return _scenario_owner(root)
    if role == "waiter":
        return _scenario_waiter(root)
    raise ValueError(role)


if __name__ == "__main__":
    sys.exit(_scenario_main())
