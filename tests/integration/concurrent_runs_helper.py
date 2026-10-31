"""別プロセスのpyfltr実行を重ねる結合試験の補助。

各実行は試験の一時領域に作成したプロジェクトで、区間の開始・終了と待機中の生存の印を区間ログへ記録する
カスタムコマンド（`slot-probe`）を実行する。保存先は`tests/conftest.py`の既定隔離が固定した`PYFLTR_CACHE_DIR`を子プロセスへ継承する。

区間ログは基準パス（`log`）と書込主体ごとの記録ファイル（`record_path`）からなる。
Windowsの追記は末尾への移動と書込が分かれており、複数のプロセスが同じファイルへ追記すると互いの行を上書きするため、
各記録ファイルへ書き込むプロセスを1つに限り、読取側が全記録ファイルをまとめて解析する。
"""

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
    """`slot-probe`を持つプロジェクトを作成する。`release`が`None`なら待たずに終了する。

    検体は`name`を書込主体とする`log`の記録ファイルへ区間を記録する。
    """
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
    """pyfltrの起動引数を返す。`script=True`ではモジュール起動の代わりにconsole scriptを使う。"""
    executable = pathlib.Path(sys.executable)
    launcher = [str(executable.with_name("pyfltr" + executable.suffix))] if script else [sys.executable, "-m", "pyfltr"]
    return [*launcher, subcommand, f"--commands={commands}", f"--output-format={output_format}", "."]


class Run:
    """別プロセスで起動したpyfltrの実行。標準出力はファイルへ、標準エラーは行単位で保持する。"""

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
        """終了と標準エラーの読み終わりを待って終了コードを返す。"""
        returncode = self.process.wait(timeout=timeout)
        self._reader.join(timeout=30)
        return returncode

    @property
    def stdout(self) -> str:
        """保存した標準出力。"""
        return self.stdout_path.read_text(encoding="utf-8")

    @property
    def stderr(self) -> str:
        """読み取り済みの標準エラー。"""
        return "".join(self.stderr_lines)

    def records(self) -> list[dict[str, typing.Any]]:
        """JSONLの標準出力をレコードの一覧として返す。"""
        return [json.loads(line) for line in self.stdout.splitlines() if line.startswith("{")]


@contextlib.contextmanager
def start(
    argv: list[str], cwd: pathlib.Path, *, stdin_path: pathlib.Path | None = None
) -> collections.abc.Generator[Run, None, None]:
    """コマンドを別プロセスで起動する。区間を抜ける時点で終了していなければ止め、パイプとファイルを閉じる。

    `stdin_path`を渡すとそのファイルを標準入力にする。
    """
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
    """条件が成立するまで待つ。

    上限に達したら、待った条件と、原因を調べるための観測（`observed`の戻り値）を示して失敗させる。
    観測は失敗の時点で取得し、待機中に届いた出力も含める。
    """
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{timeout}秒待っても「{expected}」に到達しませんでした。観測した状態:\n{observed()}")
        time.sleep(0.02)


def describe(log: pathlib.Path, *runs: Run) -> str:
    """待機の失敗の原因を調べるため、区間ログと各実行の起動引数・終了コード・出力をまとめて返す。"""
    records = _record_files(log)
    parts = [f"区間ログ {path}:\n{path.read_bytes()!r}" for path in records] or [f"区間ログ {log}.*: （未作成）"]
    for run in runs:
        parts.append(
            f"実行 {run.process.args!r}（終了コード: {run.process.poll()}）\n標準出力:\n{run.stdout}\n標準エラー:\n{run.stderr}"
        )
    return "\n".join(parts)


def record_path(log: pathlib.Path, writer: str) -> pathlib.Path:
    """書込主体`writer`が区間を記録するファイルを返す。1つのファイルへは1つのプロセスだけが書き込む。"""
    return log.with_name(f"{log.name}.{writer}")


def intervals(log: pathlib.Path) -> dict[str, tuple[float, float | None]]:
    """共有ログから実行ごとの区間（開始・終了時刻）を返す。"""
    starts: dict[str, float] = {}
    ends: dict[str, float] = {}
    for label, event, value in _events(log):
        if event == "start":
            starts[label] = value
        elif event == "end":
            ends[label] = value
    return {label: (start, ends.get(label)) for label, start in starts.items()}


def last_tick(log: pathlib.Path, label: str) -> float | None:
    """指定した実行が待機中に最後に記録した生存の印の時刻を返す。"""
    ticks = [value for name, event, value in _events(log) if name == label and event == "tick"]
    return max(ticks, default=None)


def _record_files(log: pathlib.Path) -> list[pathlib.Path]:
    return sorted(log.parent.glob(f"{glob.escape(log.name)}.*"))


def _events(log: pathlib.Path) -> list[tuple[str, str, float]]:
    events: list[tuple[str, str, float]] = []
    for path in _record_files(log):
        data = path.read_bytes()
        # 書込主体が記録中の最終行は改行が付くまで未完成のため、改行で終わる行だけを解析する。
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
    """指定した実行が区間を開始したかを返す。"""
    return label in intervals(log)


def waiting_notified(run: Run) -> bool:
    """text出力の実行が実行枠の待機を標準エラーへ通知したかを返す。"""
    return "別の検証がツールを実行中" in run.stderr


def cache_root() -> pathlib.Path:
    """子プロセスへ継承する隔離済みの保存先を返す。"""
    return pathlib.Path(os.environ["PYFLTR_CACHE_DIR"])


def header_run_id(run: Run) -> str:
    """JSONLのheaderレコードからrun_idを返す。"""
    header = next(record for record in run.records() if record.get("kind") == "header")
    return str(header["run_id"])
