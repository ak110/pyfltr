import collections.abc
import contextlib
import glob
import json
import os
import pathlib
import subprocess
import sys
import threading
import time
import typing

import tomlkit

PROBE_COMMAND = "slot-probe"

_PROBE_SCRIPT = """import pathlib
import sys
import time

log, label, release, exit_code = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
with log.open("a", encoding="utf-8") as f:
    f.write(f"{label} start {time.time()}\\n")
print(f"probe output {label}", flush=True)
deadline = time.monotonic() + 120
# 待機中は生存の印を記録する。停止された後に印が増えないことで、子の停止と枠の解放の順序を確かめられる。
while release != "-" and not pathlib.Path(release).exists():
    if time.monotonic() >= deadline:
        raise TimeoutError(release)
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{label} tick {time.time()}\\n")
    time.sleep(0.05)
with log.open("a", encoding="utf-8") as f:
    f.write(f"{label} end {time.time()}\\n")
sys.exit(exit_code)
"""


def make_project(
    root: pathlib.Path,
    name: str,
    *,
    log: pathlib.Path,
    release: pathlib.Path | None,
    exit_code: int = 0,
    extra_config: dict[str, typing.Any] | None = None,
    extra_commands: dict[str, dict[str, typing.Any]] | None = None,
) -> pathlib.Path:
    project = root / name
    project.mkdir(parents=True)
    probe = root / "probe.py"
    if not probe.exists():
        probe.write_text(_PROBE_SCRIPT, encoding="utf-8")
    (project / "sample.txt").write_text("value\n", encoding="utf-8")
    commands: dict[str, typing.Any] = {
        PROBE_COMMAND: {
            "type": "tester",
            "path": sys.executable,
            "args": [
                str(probe),
                str(record_path(log, name)),
                name,
                str(release) if release is not None else "-",
                str(exit_code),
            ],
            "targets": "*.txt",
            "pass-filenames": False,
            "fast": True,
        },
        **(extra_commands or {}),
    }
    config = {"tool": {"pyfltr": {"respect-gitignore": False, **(extra_config or {}), "custom-commands": commands}}}
    (project / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    return project


def pyfltr_argv(
    subcommand: str, *, commands: str = PROBE_COMMAND, output_format: str = "jsonl", script: bool = False
) -> list[str]:
    executable = pathlib.Path(sys.executable)
    launcher = [str(executable.with_name("pyfltr" + executable.suffix))] if script else [sys.executable, "-m", "pyfltr"]
    return [*launcher, subcommand, f"--commands={commands}", f"--output-format={output_format}", "."]


class Run:
    def __init__(self, process: subprocess.Popen[str], stdout_path: pathlib.Path) -> None:
        self.process = process
        self.stdout_path = stdout_path
        self.stderr_lines: list[str] = []
        self._reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line)

    def wait(self, timeout: float = 120) -> int:
        returncode = self.process.wait(timeout=timeout)
        self._reader.join(timeout=30)
        return returncode

    @property
    def stdout(self) -> str:
        return self.stdout_path.read_text(encoding="utf-8")

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_lines)

    def records(self) -> list[dict[str, typing.Any]]:
        return [json.loads(line) for line in self.stdout.splitlines() if line.startswith("{")]


@contextlib.contextmanager
def start(
    argv: list[str], cwd: pathlib.Path, *, stdin_path: pathlib.Path | None = None
) -> collections.abc.Generator[Run, None, None]:
    stdout_path = cwd.parent / f"{cwd.name}.stdout"
    with (
        stdout_path.open("w", encoding="utf-8") as stdout,
        stdin_path.open("rb") if stdin_path is not None else contextlib.nullcontext(subprocess.DEVNULL) as stdin,
        subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ) as process,
    ):
        run = Run(process, stdout_path)
        try:
            yield run
        finally:
            if process.poll() is None:
                process.kill()
            run.wait()


def wait_for(
    predicate: typing.Callable[[], bool], *, expected: str, observed: typing.Callable[[], str], timeout: float = 60
) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{timeout}秒待っても「{expected}」に到達しませんでした。観測した状態:\n{observed()}")
        time.sleep(0.02)


def describe(log: pathlib.Path, *runs: Run) -> str:
    records = _record_files(log)
    parts = [f"区間ログ {path}:\n{path.read_bytes()!r}" for path in records] or [f"区間ログ {log}.*: （未作成）"]
    for run in runs:
        parts.append(
            f"実行 {run.process.args!r}（終了コード: {run.process.poll()}）\n標準出力:\n{run.stdout}\n標準エラー:\n{run.stderr}"
        )
    return "\n".join(parts)


def record_path(log: pathlib.Path, writer: str) -> pathlib.Path:
    return log.with_name(f"{log.name}.{writer}")


def intervals(log: pathlib.Path) -> dict[str, tuple[float, float | None]]:
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for label, event, value in _events(log):
        if event == "start":
            starts[label] = value
        elif event == "end":
            ends[label] = value
    return {label: (start, ends.get(label)) for label, start in starts.items()}


def last_tick(log: pathlib.Path, label: str) -> float | None:
    ticks = [value for name, event, value in _events(log) if name == label and event == "tick"]
    return max(ticks, default=None)


def _record_files(log: pathlib.Path) -> list[pathlib.Path]:
    return sorted(log.parent.glob(f"{glob.escape(log.name)}.*"))


def _events(log: pathlib.Path) -> list[tuple[str, str, float]]:
    events: list[tuple[str, str, float]] = []
    for path in _record_files(log):
        data = path.read_bytes()
        for raw in data[: data.rfind(b"\n") + 1].splitlines():
            fields = raw.decode("utf-8").split()
            if len(fields) != 3:
                raise ValueError(
                    f"区間ログの行を解析できません: {path} の {raw!r}。ファイル全体: {data!r}。"
                    "このファイルへ書き込む検体やスクリプトが1つのプロセスだけで、"
                    "`<ラベル> <start|tick|end> <時刻>`の1行を改行付きで書いているかを確認してください"
                )
            label, event, value = fields
            events.append((label, event, float(value)))
    return events


def started(log: pathlib.Path, label: str) -> bool:
    return label in intervals(log)


def waiting_notified(run: Run) -> bool:
    return "別の検証が実行枠を保持しているため" in run.stderr


def cache_root() -> pathlib.Path:
    return pathlib.Path(os.environ["PYFLTR_CACHE_DIR"])


def header_run_id(run: Run) -> str:
    header = next(record for record in run.records() if record.get("kind") == "header")
    return str(header["run_id"])
