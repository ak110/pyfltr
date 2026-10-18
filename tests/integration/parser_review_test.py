"""エラーパーサーの網羅レビュー用スクリプト（`scripts/parser_review`）の採取・比較・停止を検証する。"""

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import threading
import time
import typing

import psutil
import pytest

import pyfltr.tools
from scripts.parser_review import collect, compare, process_tree, pytest_forms, samples

PROJECT_ROOT = pathlib.Path(__file__).parents[2]

# 新しいセッション（Windowsでは新しいプロセスグループ）で孫を起動し、孫のpidを出力して待機する子プロセス。
# pyfltrがツールを別セッションで起動する構成を模す。
_SPAWNER = textwrap.dedent(
    """
    import os
    import subprocess
    import sys
    import time

    kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], **kwargs)
    print(f"grandchild={grandchild.pid}", flush=True)
    # 親は孫の起動後も生存する（pyfltrがツールの終了を待つ構成を模す）。
    # releaseでは、テスト側が終了許可ファイルを作成した時点で親だけが終了する。
    release = os.path.join(os.path.dirname(os.path.abspath(__file__)), "release")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not (sys.argv[1] == "release" and os.path.exists(release)):
        time.sleep(0.05)
    """
)


def _grandchild_pid(stdout_path: str) -> int:
    for line in pathlib.Path(stdout_path).read_text(encoding="utf-8").splitlines():
        if line.startswith("grandchild="):
            return int(line.removeprefix("grandchild="))
    raise AssertionError("孫のpidが出力されていない")


def _terminated(pid: int) -> bool:
    try:
        process = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    return process.status() == psutil.STATUS_ZOMBIE


def _run_spawner(tmp_path: pathlib.Path, mode: str, **kwargs) -> process_tree.TrackedResult:
    script = tmp_path / "spawner.py"
    script.write_text(_SPAWNER, encoding="utf-8")
    return process_tree.run_tracked(
        [sys.executable, str(script), mode],
        cwd=tmp_path,
        env=None,
        stdout_path=tmp_path / "out.txt",
        stderr_path=tmp_path / "err.txt",
        grace=2.0,
        **kwargs,
    )


# 追跡側が孫を子孫として登録するまで待つ上限秒数。試験の時間上限（120秒）より十分短くする。
_REGISTRATION_WAIT = 30.0


class _RegistrationWatch:
    """追跡側（`process_tree._Tracker`）が子孫として登録したpidを試験へ通知する。

    `run_tracked`は呼び出しのたびに`process_tree._Tracker`を名前解決して生成するため、
    登録時に通知する派生クラスへ差し替えれば、公開引数を増やさずに登録の時点を観測できる。
    """

    def __init__(self) -> None:
        self._registered: set[int] = set()
        self._condition = threading.Condition()

    def tracker_class(self) -> type[process_tree._Tracker]:  # pylint: disable=protected-access
        watch = self

        class _NotifyingTracker(process_tree._Tracker):  # pylint: disable=protected-access
            def _add(self, process: psutil.Process) -> None:
                super()._add(process)
                with watch._condition:  # pylint: disable=protected-access
                    watch._registered.add(process.pid)  # pylint: disable=protected-access
                    watch._condition.notify_all()  # pylint: disable=protected-access

        return _NotifyingTracker

    def wait_registered(self, pid: int, timeout: float) -> bool:
        """`pid`の登録を最大`timeout`秒待ち、登録を観測できたかを返す。"""
        with self._condition:
            return self._condition.wait_for(lambda: pid in self._registered, timeout=timeout)


@pytest.fixture(name="registration_watch")
def _registration_watch(monkeypatch: pytest.MonkeyPatch) -> _RegistrationWatch:
    watch = _RegistrationWatch()
    monkeypatch.setattr(process_tree, "_Tracker", watch.tracker_class())
    return watch


def _wait_grandchild_pid(path: pathlib.Path, timeout: float) -> int | None:
    """孫のpidの出力を最大`timeout`秒待って返す。出力されなければNoneを返す。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("grandchild="):
                    return int(line.removeprefix("grandchild="))
        time.sleep(0.05)
    return None


def _after_grandchild_registered(path: pathlib.Path, watch: _RegistrationWatch, action: typing.Callable[[], None]) -> list[str]:
    """孫のpidが出力され、追跡側が孫を登録したことを観測してから`action`を呼ぶ。

    観測できない場合も、試験の実行を終端へ進めるため`action`を呼び、理由を返り値のリストへ記録する。
    試験は実行後にこのリストが空であることを確かめる。
    """
    failures: list[str] = []

    def _watch() -> None:
        grandchild = _wait_grandchild_pid(path, _REGISTRATION_WAIT)
        if grandchild is None:
            failures.append(f"孫のpidが{_REGISTRATION_WAIT}秒以内に出力されなかった")
        elif not watch.wait_registered(grandchild, _REGISTRATION_WAIT):
            failures.append(f"追跡側が孫(pid={grandchild})を{_REGISTRATION_WAIT}秒以内に登録しなかった")
        action()

    threading.Thread(target=_watch, daemon=True).start()
    return failures


class _ClockHeldUntilRegistered:
    """孫の登録を観測するまで進まない`process_tree`用の時計。

    `run_tracked`は時間上限の計時を`time.monotonic()`で行う。登録を観測するまで同じ値を返し、
    観測後は観測時点からの経過だけを進めることで、孫の起動・出力・登録が負荷で遅れても
    登録より前に時間上限へ達しない。登録を上限内に観測できない場合は時計を進め、
    実行を終わらせたうえで理由を`failures`へ記録する。
    """

    def __init__(self, path: pathlib.Path, watch: _RegistrationWatch) -> None:
        self._held_at = time.monotonic()
        self._offset: float | None = None
        self._lock = threading.Lock()

        def _release() -> None:
            with self._lock:
                self._offset = time.monotonic() - self._held_at

        self.failures: list[str] = _after_grandchild_registered(path, watch, _release)

    def monotonic(self) -> float:
        with self._lock:
            if self._offset is None:
                return self._held_at
            return time.monotonic() - self._offset

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_timeout_stops_descendants_and_keeps_partial_output(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, registration_watch: _RegistrationWatch
) -> None:
    """時間上限で停止した実行は別セッションの孫まで終端させ、途中出力と停止の記録を残す。

    時間上限の計時は追跡側が孫を登録した時点から進める（`_ClockHeldUntilRegistered`）。
    """
    clock = _ClockHeldUntilRegistered(tmp_path / "out.txt", registration_watch)
    monkeypatch.setattr(process_tree, "time", clock)
    result = _run_spawner(tmp_path, "wait", timeout=5)

    assert not clock.failures, clock.failures
    assert result.outcome == "timeout"
    grandchild = _grandchild_pid(result.stdout_path)
    assert grandchild in {item["pid"] for item in result.descendants}
    assert grandchild in result.stopped
    assert not result.stop_failed
    assert _terminated(grandchild)


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_interrupt_stops_descendants(tmp_path: pathlib.Path, registration_watch: _RegistrationWatch) -> None:
    """中断要求で停止した実行は孫まで終端させ、中断として記録する。"""
    stop_event = threading.Event()
    failures = _after_grandchild_registered(tmp_path / "out.txt", registration_watch, stop_event.set)
    result = _run_spawner(tmp_path, "wait", timeout=90, stop_event=stop_event)

    assert not failures, failures
    assert result.outcome == "interrupted"
    grandchild = _grandchild_pid(result.stdout_path)
    assert grandchild in result.stopped
    assert not result.stop_failed
    assert _terminated(grandchild)


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_leftover_descendant_after_exit_is_stopped(tmp_path: pathlib.Path, registration_watch: _RegistrationWatch) -> None:
    """親が自然終了した後に残った孫も停止し、停止したことを記録する。"""
    failures = _after_grandchild_registered(tmp_path / "out.txt", registration_watch, (tmp_path / "release").touch)
    result = _run_spawner(tmp_path, "release", timeout=90)

    assert not failures, failures
    assert result.outcome == "exited"
    assert result.returncode == 0
    grandchild = _grandchild_pid(result.stdout_path)
    assert grandchild in result.stopped
    assert _terminated(grandchild)


def test_every_builtin_command_has_sample() -> None:
    """全`BUILTIN_COMMANDS`にサンプル定義があり、未知のツール名は採取前に拒否する。"""
    assert samples.missing_samples() == []
    assert [sample.tool for sample in samples.select_samples(None)] == list(pyfltr.tools.BUILTIN_COMMANDS)
    with pytest.raises(ValueError, match="未知のツール名"):
        samples.select_samples(["no-such-tool"])


@pytest.fixture(name="isolated_archive", autouse=True)
def _isolated_archive(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """採取が起動するpyfltrの実行アーカイブを利用者のキャッシュから分ける。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path / "pyfltr-cache"))


def _collector(tmp_path: pathlib.Path, *, timeout: float = 300.0) -> collect.Collector:
    return collect.Collector(output_dir=tmp_path / "collected", timeout=timeout, total_timeout=600.0)


@pytest.mark.timeout(300)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_collect_and_compare_selected_tools(tmp_path: pathlib.Path) -> None:
    """採取は生出力・診断・コマンドラインと状態を残し、比較は両版の診断全件と差分を出力する。"""
    selected = samples.select_samples(["ruff-format", "ruff-check"])
    collector = _collector(tmp_path)
    records = collect.collect(collector, selected)
    summary = collect.write_results(collector.output_dir, records, incomplete=collector.incomplete)

    by_tool = {record["tool"]: record for record in records}
    assert by_tool["ruff-format"]["state"] == collect.STATE_NO_PARSER
    check = by_tool["ruff-check"]
    assert check["state"] == collect.STATE_COMPARABLE
    assert check["diagnostic_count"] >= 1
    assert "F401" in pathlib.Path(check["output"]).read_text(encoding="utf-8")
    assert check["commandline"]
    assert pathlib.Path(check["run_jsonl"]) == collector.output_dir / "ruff-check" / "run.jsonl"
    assert pathlib.Path(check["run_jsonl"]).is_file()
    assert summary["counts"] == {collect.STATE_NO_PARSER: 1, collect.STATE_COMPARABLE: 1}
    inputs = [
        json.loads(line) for line in (collector.output_dir / collect.INPUTS_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert [item["id"] for item in inputs] == ["ruff-check"]

    # 公開された呼び出し手段で比較する。
    report_path = tmp_path / "compare.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.parser_review",
            "compare",
            "--input",
            str(collector.output_dir),
            "--output",
            str(report_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["base"]["pyfltr_root"] != report["current"]["pyfltr_root"]
    assert pathlib.Path(report["current"]["pyfltr_root"]) == PROJECT_ROOT
    (entry,) = report["entries"]
    assert entry["id"] == "ruff-check"
    assert entry["output"] == check["output"]
    assert isinstance(entry["base"], list) and isinstance(entry["current"], list)
    assert entry["result"] in {"equal", "different"}


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_collect_timeout_is_not_counted_as_success(tmp_path: pathlib.Path) -> None:
    """時間上限で止まった採取は未採取となり、比較の入力へ含めない。"""
    collector = _collector(tmp_path, timeout=0.01)
    records = collect.collect(collector, samples.select_samples(["ruff-check"]))
    collect.write_results(collector.output_dir, records, incomplete=collector.incomplete)

    (record,) = records
    assert record["state"] == collect.STATE_NOT_COLLECTED
    assert "時間上限" in record["reason"]
    assert record["execution"]["outcome"] == "timeout"
    assert not record["execution"]["stop_failed"]
    assert (collector.output_dir / collect.INPUTS_FILENAME).read_text(encoding="utf-8") == ""


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_collect_total_timeout_marks_incomplete(tmp_path: pathlib.Path) -> None:
    """採取全体の時間上限で打ち切られた実行は、最後の1件でも採取全体を不完全とし終了コード1につながる。"""
    # 全体の上限はpyfltrの起動（Pythonインタープリターの起動を含む）より十分短くし、
    # 起動の高速化で実行が上限内に完了して前提が崩れることを避ける。
    collector = collect.Collector(output_dir=tmp_path / "collected", timeout=300.0, total_timeout=0.05)
    records = collect.collect(collector, samples.select_samples(["ruff-check"]))

    (record,) = records
    assert record["state"] == collect.STATE_NOT_COLLECTED
    assert "全体の時間上限" in record["reason"]
    assert collector.incomplete


def test_collect_interrupt_marks_remaining_samples(tmp_path: pathlib.Path) -> None:
    """中断要求の後のサンプルは実行せず未採取として記録し、採取全体を不完全とする。"""
    collector = _collector(tmp_path)
    collector.stop_event.set()
    records = collect.collect(collector, samples.select_samples(["ruff-check", "mypy"]))

    assert [record["state"] for record in records] == [collect.STATE_NOT_COLLECTED] * 2
    assert collector.incomplete


def test_compare_uses_each_input_start_cwd(tmp_path: pathlib.Path) -> None:
    """各入力の起点cwdで基準版と現行版を解析し、絶対パスのフレーム選択を揃える。"""
    rows = []
    for name in ("first", "second"):
        cwd = tmp_path / name
        cwd.mkdir()
        project = cwd / "sample_test.py"
        project.write_text("def test_sample():\n    pass\n", encoding="utf-8")
        output = cwd / "output.log"
        output.write_text(
            "===== FAILURES =====\n_____ test_sample _____\n"
            f"{project.as_posix()}:3: in test_sample\n    call()\n"
            f"{(tmp_path / 'external/lib.py').as_posix()}:5: in call\nE   ValueError: sample\n",
            encoding="utf-8",
        )
        rows.append({"id": name, "tool": "pytest", "output": str(output), "path_base": str(cwd), "start_cwd": str(cwd)})
    inputs = tmp_path / "inputs.jsonl"
    inputs.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    report = compare.compare(tmp_path, tmp_path / "compare.json", base="HEAD")
    for entry in report["entries"]:
        for side in ("base", "current"):
            assert [(error["file"], error["line"]) for error in entry[side]] == [("sample_test.py", 3)]


def test_diff_diagnostics_compares_all_fields() -> None:
    """全フィールドを比較し、片側だけの診断と順序だけの差を区別する。"""
    first = {"file": "a.py", "line": 1, "message": "m", "end_line": None}
    second = {"file": "a.py", "line": 2, "message": "m", "end_line": None}
    changed = dict(first, end_line=3)

    assert compare.diff_diagnostics([first], [first])["equal"]
    order = compare.diff_diagnostics([first, second], [second, first])
    assert order["order_changed"] and not order["only_base"] and not order["only_current"]
    diff = compare.diff_diagnostics([first, second], [changed, second])
    assert diff["only_base"] == [first]
    assert diff["only_current"] == [changed]


def test_pytest_forms_cover_reviewer_definition() -> None:
    """形態1〜21と、レビュー担当定義が求める派生を全て生成対象に持つ。"""
    forms = pytest_forms.FORMS
    assert {form.number for form in forms} == set(range(1, 22))
    ids = {form.id for form in forms}
    assert len(ids) == len(forms)
    assert sum(1 for form in forms if form.number == 7) == 10
    assert sum(1 for form in forms if form.number == 11) == 4
    assert {form.sample.env.get("COLUMNS") for form in forms if form.number == 8 and form.sample} == {"80", "81"}
    assert sum(1 for form in forms if form.number == 12) == 2
    assert sum(1 for form in forms if form.number == 13 and form.derivation) == 2
    for form in forms:
        if form.derivation is not None:
            assert set(form.derivation.sources) <= ids
    tb_auto = [form for form in forms if form.sample and form.sample.settings.get("pytest-tb-line") is False]
    assert {form.number for form in tb_auto} == {8, 11, 16, 17, 19, 20, 21}
    assert sum(form.number == 19 for form in forms) == 4
    assert sum(form.number == 20 for form in forms) == 5


@pytest.mark.timeout(300)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_pytest_forms_generate_real_and_derived_inputs(tmp_path: pathlib.Path) -> None:
    """実出力の形態と派生入力を生成し、派生入力は派生元と変換内容を持つ。"""
    forms = pytest_forms.select_forms(["pytest-12-truncated-with-summary-list"])
    assert [form.id for form in forms] == ["pytest-02-class-based", "pytest-12-truncated-with-summary-list"]
    collector = _collector(tmp_path)
    records = pytest_forms.generate(collector, forms)
    collect.write_results(collector.output_dir, records, incomplete=collector.incomplete)

    source, derived = records
    assert source["state"] == collect.STATE_COMPARABLE
    assert derived["state"] == collect.STATE_COMPARABLE
    assert derived["derived_from"] == ["pytest-02-class-based"]
    assert derived["transform"]
    source_output = pathlib.Path(source["output"]).read_text(encoding="utf-8")
    derived_output = pathlib.Path(derived["output"]).read_text(encoding="utf-8")
    assert derived_output != source_output
    assert source_output.startswith(derived_output.rstrip("\n"))
    inputs = [
        json.loads(line) for line in (collector.output_dir / collect.INPUTS_FILENAME).read_text(encoding="utf-8").splitlines()
    ]
    assert inputs[1]["derived_from"] == ["pytest-02-class-based"]
    assert inputs[1]["path_base"] == source["cwd"]


def test_cli_rejects_unknown_tool() -> None:
    """未知のツール名を指定した採取は何も実行せず終了コード2で終わる。"""
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.parser_review", "collect", "--tools", "no-such-tool", "--output", os.devnull],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 2
    assert "未知のツール名" in completed.stderr
