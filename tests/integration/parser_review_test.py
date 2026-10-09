"""エラーパーサーの網羅レビュー用スクリプト（`scripts/parser_review`）の採取・比較・停止を検証する。"""

import json
import os
import pathlib
import subprocess
import sys
import textwrap
import threading
import time

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


def _after_grandchild_line(path: pathlib.Path, action) -> None:
    """孫のpidが出力された後、追跡側の列挙（0.1秒間隔）が十分に巡る時間をおいて`action`を呼ぶ。"""

    def _watch() -> None:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if path.exists() and "grandchild=" in path.read_text(encoding="utf-8"):
                time.sleep(2.0)
                action()
                return
            time.sleep(0.05)

    threading.Thread(target=_watch, daemon=True).start()


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_timeout_stops_descendants_and_keeps_partial_output(tmp_path: pathlib.Path) -> None:
    """時間上限で停止した実行は別セッションの孫まで終端させ、途中出力と停止の記録を残す。

    時間上限は起動時点から計るため、孫の起動と出力が負荷で遅れても上限前に到達できる長さにする。
    """
    result = _run_spawner(tmp_path, "wait", timeout=15)

    assert result.outcome == "timeout"
    grandchild = _grandchild_pid(result.stdout_path)
    assert grandchild in {item["pid"] for item in result.descendants}
    assert grandchild in result.stopped
    assert not result.stop_failed
    assert _terminated(grandchild)


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_interrupt_stops_descendants(tmp_path: pathlib.Path) -> None:
    """中断要求で停止した実行は孫まで終端させ、中断として記録する。"""
    stop_event = threading.Event()
    _after_grandchild_line(tmp_path / "out.txt", stop_event.set)
    result = _run_spawner(tmp_path, "wait", timeout=90, stop_event=stop_event)

    assert result.outcome == "interrupted"
    grandchild = _grandchild_pid(result.stdout_path)
    assert grandchild in result.stopped
    assert not result.stop_failed
    assert _terminated(grandchild)


@pytest.mark.timeout(120)
@pytest.mark.usefixtures("_disable_faulthandler_timeout")
def test_leftover_descendant_after_exit_is_stopped(tmp_path: pathlib.Path) -> None:
    """親が自然終了した後に残った孫も停止し、停止したことを記録する。"""
    _after_grandchild_line(tmp_path / "out.txt", (tmp_path / "release").touch)
    result = _run_spawner(tmp_path, "release", timeout=90)

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
    collector = collect.Collector(output_dir=tmp_path / "collected", timeout=300.0, total_timeout=0.5)
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
    """形態1〜14と、レビュー担当定義が求める派生を全て生成対象に持つ。"""
    forms = pytest_forms.FORMS
    assert {form.number for form in forms} == set(range(1, 15))
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
    assert {form.number for form in tb_auto} == {8, 11}


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
