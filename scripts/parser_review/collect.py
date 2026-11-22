import collections.abc
import contextlib
import dataclasses
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import typing

import pyfltr.command.core_
import pyfltr.command.execution_slot
import pyfltr.command.stage_runner
import pyfltr.state.archive
from scripts.parser_review import capture_run, process_tree
from scripts.parser_review.samples import Sample, has_parse_definition, pyfltr_settings, render_files

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

STATE_COMPARABLE = "comparable"
"""生出力を取得し、解析定義を持つため二版比較の入力になる。"""
STATE_NO_PARSER = "no_parser"
"""生出力を取得したが、ツールが解析定義を持たない。"""
STATE_NOT_COLLECTED = "not_collected"
"""採取が成立しなかった（条件不足・失敗・時間上限・中断・停止失敗）。パース成功に数えない。"""

_NOT_RUN_STATUSES = frozenset({"resolution_failed", "skipped"})
INPUTS_FILENAME = "inputs.jsonl"
SUMMARY_FILENAME = "summary.json"
_CAPTURE_RUN = pathlib.Path(capture_run.__file__).resolve()
_PARSE_INPUTS_FILENAME = "parse_inputs.json"


@dataclasses.dataclass
class Collector:
    output_dir: pathlib.Path
    timeout: float
    total_timeout: float
    stop_event: threading.Event = dataclasses.field(default_factory=threading.Event)
    archive: pyfltr.state.archive.ArchiveStore = dataclasses.field(default_factory=pyfltr.state.archive.ArchiveStore)
    started: float = dataclasses.field(default_factory=time.monotonic)
    incomplete: bool = False
    """中断・全体の時間上限・停止失敗のいずれかが起きたか。"""

    def remaining(self) -> float:
        return self.total_timeout - (time.monotonic() - self.started)

    def step_timeout(self) -> float | None:
        remaining = self.remaining()
        if remaining <= 0:
            return None
        return min(self.timeout, remaining)

    @contextlib.contextmanager
    def execution_slot(self) -> collections.abc.Generator[None, None, None]:
        try:
            with pyfltr.command.execution_slot.hold(
                label="parser_review", should_abort=self.stop_event.is_set, on_wait=_report_slot_wait
            ):
                self.started = time.monotonic()
                yield
        except pyfltr.command.execution_slot.WaitAborted:
            self.incomplete = True
            yield


def _report_slot_wait(owner: pyfltr.command.execution_slot.SlotOwner | None) -> None:
    holder = pyfltr.command.stage_runner.describe_slot_owner(owner)
    records = pyfltr.command.stage_runner.describe_slot_owner_records([owner] if owner is not None else [])
    print(
        f"検証の実行枠が空くのを待っています（保持者: {holder}）。時間の上限は取得後から数えます。"
        f"待たずに止める場合はCtrl+Cで中断してください。{records}",
        file=sys.stderr,
        flush=True,
    )


def _pyfltr_env(sample: Sample) -> dict[str, str]:
    env = dict(os.environ)
    env.update(sample.env)
    return env


def _pyfltr_argv(sample: Sample) -> list[str]:
    return [
        "uv",
        "run",
        "--project",
        str(REPO_ROOT),
        "--frozen",
        "--no-sync",
        "python",
        str(_CAPTURE_RUN),
        "run",
        f"--commands={sample.tool}",
        "--no-fix",
        "--no-cache",
        "--no-clear",
        "--output-format=jsonl",
        *sample.targets,
    ]


def _write_sample(sample: Sample, sample_dir: pathlib.Path) -> None:
    if sample_dir.exists():
        shutil.rmtree(sample_dir)
    sample_dir.mkdir(parents=True)
    for relative, content in render_files(sample).items():
        path = sample_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _git_init(sample_dir: pathlib.Path) -> None:
    for argv in (
        ["git", "init", "--quiet"],
        ["git", "add", "--all"],
        [
            "git",
            "-c",
            "user.email=parser-review@example.com",
            "-c",
            "user.name=parser-review",
            "commit",
            "--quiet",
            "-m",
            "sample",
        ],
    ):
        subprocess.run(argv, cwd=sample_dir, check=True, capture_output=True, timeout=60)


def _parse_jsonl(path: pathlib.Path) -> tuple[list[dict[str, typing.Any]], list[str]]:
    records: list[dict[str, typing.Any]] = []
    invalid: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="backslashreplace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            invalid.append(line)
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, invalid


def _final_command_record(records: list[dict[str, typing.Any]], tool: str) -> dict[str, typing.Any] | None:
    for record in reversed(records):
        if record.get("kind") == "command" and record.get("command") == tool:
            return record
    return None


def _not_collected(
    record: dict[str, typing.Any], reason: str, action: str = "実行記録で中断・時間上限・停止失敗を確認してください"
) -> dict[str, typing.Any]:
    record["state"] = STATE_NOT_COLLECTED
    record["reason"] = reason
    retry = (
        f"pytest-forms --forms={record['id']}"
        if str(record["id"]).startswith("pytest-")
        else f"collect --tools={record['tool']}"
    )
    record["hint"] = f"{action}。{retry}へ同じ--outputを指定して再採取してください。"
    return record


def _run_step(
    collector: Collector,
    argv: list[str],
    *,
    cwd: pathlib.Path,
    env: dict[str, str],
    stdout_path: pathlib.Path,
    stderr_path: pathlib.Path,
) -> process_tree.TrackedResult | None:
    step_timeout = collector.step_timeout()
    if step_timeout is None:
        return None
    return process_tree.run_tracked(
        argv,
        cwd=cwd,
        env=env,
        timeout=step_timeout,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stop_event=collector.stop_event,
    )


def _execution_failure(collector: Collector, result: process_tree.TrackedResult | None) -> str | None:
    if result is None:
        collector.incomplete = True
        return "全体の時間上限に達したため実行しなかった"
    if result.stop_failed:
        collector.incomplete = True
        return f"停止後も終端を確認できないプロセスが残った: pids={result.survivors}"
    if result.outcome == "timeout":
        if collector.remaining() <= 0:
            collector.incomplete = True
            return f"全体の時間上限に達して停止した（{result.elapsed}秒）"
        return f"時間上限（{result.elapsed}秒）に達して停止した"
    if result.outcome == "interrupted":
        collector.incomplete = True
        return "中断要求により停止した"
    if result.outcome == "start_failed":
        return f"起動に失敗した: {result.error}"
    return None


def collect_sample(collector: Collector, sample: Sample) -> dict[str, typing.Any]:
    work_dir = collector.output_dir / sample.id
    sample_dir = work_dir / "sample"
    work_dir.mkdir(parents=True, exist_ok=True)
    record: dict[str, typing.Any] = {
        "id": sample.id,
        "tool": sample.tool,
        "description": sample.description,
        "has_parse_definition": has_parse_definition(sample.tool),
        "cwd": str(sample_dir),
        "settings": pyfltr_settings(sample),
        "env": dict(sample.env),
        "targets": list(sample.targets),
        "files": sorted(render_files(sample)),
    }
    if collector.stop_event.is_set():
        collector.incomplete = True
        return _not_collected(record, "中断要求により実行しなかった")
    _write_sample(sample, sample_dir)
    env = _pyfltr_env(sample)
    parse_inputs_path = work_dir / _PARSE_INPUTS_FILENAME
    parse_inputs_path.unlink(missing_ok=True)
    try:
        if sample.git:
            _git_init(sample_dir)
    except (OSError, subprocess.SubprocessError) as exc:
        return _not_collected(
            record, f"git準備に失敗した: {exc}", "gitの導入状態とサンプルディレクトリの書込権限を確認してください"
        )

    record["setup"] = []
    for index, argv in enumerate(sample.setup):
        setup_env = {key: value for key, value in env.items() if key != "UV_FROZEN"}
        result = _run_step(
            collector,
            list(argv),
            cwd=sample_dir,
            env=setup_env,
            stdout_path=work_dir / f"setup{index}.stdout",
            stderr_path=work_dir / f"setup{index}.stderr",
        )
        if result is not None:
            record["setup"].append(result.to_dict())
        failure = _execution_failure(collector, result)
        if failure is None and result is not None and result.returncode != 0:
            failure = f"終了コード{result.returncode}で失敗した"
        if failure is not None:
            return _not_collected(
                record,
                f"準備コマンド{' '.join(argv)}が成立しなかった（条件不足）: {failure}",
                "setupのstdout・stderrを読み、必要なランタイム・ネットワーク・認証の条件を整えてください",
            )

    result = _run_step(
        collector,
        _pyfltr_argv(sample),
        cwd=sample_dir,
        env={**env, capture_run.CAPTURE_ENV: str(parse_inputs_path)},
        stdout_path=work_dir / "run.jsonl",
        stderr_path=work_dir / "run.stderr",
    )
    if result is not None:
        record["execution"] = result.to_dict()
        record["run_jsonl"] = result.stdout_path
    failure = _execution_failure(collector, result)
    if failure is not None:
        return _not_collected(record, failure)
    assert result is not None

    records, invalid = _parse_jsonl(pathlib.Path(result.stdout_path))
    record["exit_code"] = result.returncode
    if invalid:
        record["invalid_jsonl_lines"] = len(invalid)
    header = next((item for item in records if item.get("kind") == "header"), None)
    if header is None or not header.get("run_id"):
        return _not_collected(
            record,
            "JSON Linesにrun_idを持つheaderが無い（pyfltrの異常終了）",
            "run.jsonlとrun.stderrを読み、pyfltrの設定・依存関係のエラーを修正してください",
        )
    run_id = str(header["run_id"])
    record["run_id"] = run_id
    command = _final_command_record(records, sample.tool)
    try:
        output = collector.archive.read_tool_output(run_id, sample.tool)
        diagnostics = collector.archive.read_tool_diagnostics(run_id, sample.tool)
        meta = collector.archive.read_tool_meta(run_id, sample.tool)
    except FileNotFoundError as exc:
        status = command.get("status") if command is not None else None
        record["status"] = status
        if status in _NOT_RUN_STATUSES:
            return _not_collected(
                record,
                f"ツールが実行されなかった（status={status}。未導入・外部条件の不足など）",
                f"pyfltr command-info {sample.tool} --checkで解決状態を確認し、必要なツールと設定を用意してください",
            )
        return _not_collected(
            record,
            f"実行アーカイブに記録が無い: {exc}",
            "run.stderrの初期化エラーを読み、アーカイブ保存先の書込権限と空き容量を確認してください",
        )
    status = meta.get("status")
    record["status"] = status
    if status in _NOT_RUN_STATUSES:
        return _not_collected(
            record,
            f"ツールが実行されなかった（status={status}。未導入・外部条件の不足など）",
            f"pyfltr command-info {sample.tool} --checkで解決状態を確認し、必要なツールと設定を用意してください",
        )
    output_path = work_dir / "output.log"
    output_path.write_text(output, encoding="utf-8")
    (work_dir / "diagnostics.json").write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    (work_dir / "tool_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    record["output"] = str(output_path)
    record["commandline"] = meta.get("commandline")
    record["diagnostic_count"] = sum(len(item.get("messages", [])) for item in diagnostics)
    parse_input = _single_parse_input(parse_inputs_path)
    if parse_input is not None:
        parse_input_path = work_dir / "parse_input.log"
        parse_input_path.write_text(parse_input, encoding="utf-8")
        record["parse_input"] = str(parse_input_path)
    if result.stopped:
        record["stopped_leftover_pids"] = result.stopped
    missing_args = _missing_args(record["settings"], sample.tool, record["commandline"])
    if missing_args:
        return _not_collected(
            record,
            f"サンプルが指定した引数{missing_args}が実際のコマンドラインに無い（pyfltrの注入で置き換えられた）",
            "tool_meta.jsonのcommandlineとpyfltrの注入規則を読み、サンプルの設定を意図した引数が渡る形へ直してください",
        )
    aborted = _aborted_before_inspection(sample.tool, output)
    if aborted is not None:
        return _not_collected(
            record,
            f"ツールが検査の前に異常終了した: {aborted}",
            "output.logを読み、ツールの実行条件（Pythonの版・資源の上限・GitLabのremoteなど）を整えてください",
        )
    record["state"] = STATE_COMPARABLE if record["has_parse_definition"] else STATE_NO_PARSER
    if (
        record["state"] == STATE_COMPARABLE
        and record["diagnostic_count"] == 0
        and pyfltr.command.core_.is_failed_status(status)
    ):
        record["attention"] = "ツールは失敗したが診断が0件。外部条件の不足か解析の欠落かを生出力で確認する"
    return record


def _single_parse_input(path: pathlib.Path) -> str | None:
    if not path.is_file():
        return None
    inputs = json.loads(path.read_text(encoding="utf-8"))
    if len(inputs) != 1:
        return None
    return inputs[0]


def _missing_args(settings: dict[str, typing.Any], tool: str, commandline: list[str] | None) -> list[str]:
    actual = set(commandline or [])
    return [str(arg) for arg in settings.get(f"{tool}-args", []) if str(arg) not in actual]


def _aborted_before_inspection(tool: str, output: str) -> str | None:
    if tool == "glab-ci-lint":
        marker = "you must be in a GitLab project repository"
        return marker if marker in output else None
    if tool != "semgrep":
        return None
    for line in output.splitlines():
        if not line.startswith("{"):
            continue
        try:
            report = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(report, dict) or report.get("results"):
            continue
        errors = [error for error in report.get("errors", []) if isinstance(error, dict) and error.get("level") == "error"]
        if errors:
            return " / ".join(" ".join(str(error.get("message", "")).split())[:200] for error in errors)
    return None


def comparison_output(record: dict[str, typing.Any]) -> str:
    return record.get("parse_input") or record["output"]


def comparison_input(record: dict[str, typing.Any]) -> dict[str, typing.Any]:
    return {
        "id": record["id"],
        "tool": record["tool"],
        "output": comparison_output(record),
        "path_base": record["cwd"],
        "start_cwd": record["cwd"],
        "diagnostic_count": record.get("diagnostic_count"),
        "derived_from": record.get("derived_from"),
        "transform": record.get("transform"),
    }


def write_results(output_dir: pathlib.Path, records: list[dict[str, typing.Any]], *, incomplete: bool) -> dict[str, typing.Any]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["state"]] = counts.get(record["state"], 0) + 1
        (output_dir / record["id"]).mkdir(parents=True, exist_ok=True)
        (output_dir / record["id"] / "record.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    with (output_dir / INPUTS_FILENAME).open("w", encoding="utf-8") as f:
        for record in records:
            if record["state"] == STATE_COMPARABLE:
                f.write(json.dumps(comparison_input(record), ensure_ascii=False) + "\n")
    summary = {
        "incomplete": incomplete,
        "counts": counts,
        "total": len(records),
        "entries": [
            {
                key: record.get(key)
                for key in (
                    "id",
                    "tool",
                    "state",
                    "reason",
                    "hint",
                    "status",
                    "diagnostic_count",
                    "attention",
                    "derived_from",
                    "transform",
                )
                if record.get(key) is not None
            }
            for record in records
        ],
    }
    (output_dir / SUMMARY_FILENAME).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def collect(collector: Collector, samples: list[Sample]) -> list[dict[str, typing.Any]]:
    collector.output_dir.mkdir(parents=True, exist_ok=True)
    with collector.execution_slot():
        return [collect_sample(collector, sample) for sample in samples]
