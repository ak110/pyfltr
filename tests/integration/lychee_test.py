import json
import pathlib
import subprocess
import sys
import types
import typing

import pytest

import pyfltr.cli.main
import pyfltr.cli.mcp_server
import pyfltr.command.lychee
import pyfltr.command.process
import pyfltr.state.archive


def _report(*codes: int | str, scheme: str = "https") -> str:
    errors = []
    timeouts = []
    for index, code in enumerate(codes):
        status = {"text": "Timeout", "details": "Request timed out"} if code == "Timeout" else {"text": str(code), "code": code}
        entry = {"url": f"{scheme}://example.com/link-{index}", "status": status}
        if code == "Timeout":
            timeouts.append(entry)
        else:
            errors.append(entry)
    report = json.dumps(
        {
            "errors": len(errors),
            "timeouts": len(timeouts),
            "unknown": 0,
            "unsupported": 0,
            "error_map": {"links.md": errors} if errors else {},
            "timeout_map": {"links.md": timeouts} if timeouts else {},
        }
    )
    return report + "\nHint: You can configure accepted/rejected response codes with `-a` or `--accept`\n"


@pytest.fixture(name="lychee_project")
def _lychee_project(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    """外部プロセスと時計だけを差し替える検証先を用意する。"""
    (tmp_path / "pyproject.toml").write_text(
        f"[tool.pyfltr]\nlychee-path = {json.dumps(sys.executable)}\nrespect-gitignore = false\njobs = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "links.md").write_text("[link](https://example.com)\n", encoding="utf-8")
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path / "cache"))
    clock = [0.0]

    def _sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(pyfltr.command.lychee, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=_sleep))
    return tmp_path


def _run_cli(project: pathlib.Path, *, output_format: str = "jsonl") -> int:
    return pyfltr.cli.main.run(
        ["ci", "--work-dir", str(project), "--commands=lychee", f"--output-format={output_format}", "--no-cache", str(project)]
    )


@pytest.mark.parametrize(
    ("codes", "scheme", "expected_status", "expected_exit", "expected_severities", "attempts"),
    [
        ((503,), "https", "warning", 0, ["warning"], 4),
        ((500, 502, 504), "https", "warning", 0, ["warning", "warning", "warning"], 4),
        (("Timeout",), "https", "warning", 0, ["warning"], 1),
        ((404,), "https", "failed", 1, ["error"], 1),
        ((503, 404, "Timeout"), "https", "failed", 1, ["warning", "error", "warning"], 4),
        ((429,), "https", "failed", 1, ["error"], 1),
        ((499,), "https", "failed", 1, ["error"], 1),
        ((599,), "http", "warning", 0, ["warning"], 4),
        ((600,), "https", "failed", 1, ["error"], 1),
        ((503,), "file", "failed", 1, ["error"], 1),
        (("Timeout",), "file", "failed", 1, ["error"], 1),
    ],
)
def test_lychee_cli_classification(
    lychee_project: pathlib.Path,
    mocker: typing.Any,
    capsys: pytest.CaptureFixture[str],
    codes: tuple[int | str, ...],
    scheme: str,
    expected_status: str,
    expected_exit: int,
    expected_severities: list[str],
    attempts: int,
) -> None:
    """一時障害を警告へ分類し、永久失敗をJSONLとアーカイブで保持する。"""
    process = mocker.patch(
        "pyfltr.command.process.run_subprocess",
        return_value=subprocess.CompletedProcess([], 2, _report(*codes, scheme=scheme)),
    )
    assert _run_cli(lychee_project) == expected_exit
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    command = next(record for record in records if record["kind"] == "command")
    header = next(record for record in records if record["kind"] == "header")
    assert command["status"] == expected_status
    store = pyfltr.state.archive.ArchiveStore(cache_root=lychee_project / "cache")
    archived = store.read_tool_meta(header["run_id"], "lychee")
    assert archived["status"] == expected_status
    diagnostics = store.read_tool_diagnostics(header["run_id"], "lychee")
    messages = [message for diagnostic in diagnostics for message in diagnostic["messages"]]
    assert [message["severity"] for message in messages] == expected_severities
    assert all(f"{scheme}://example.com/link-" in message["msg"] for message in messages)
    assert process.call_count == attempts


def test_lychee_cli_recovers(lychee_project: pathlib.Path, mocker: typing.Any, capsys: pytest.CaptureFixture[str]) -> None:
    """途中復旧後は古い失敗を診断とアーカイブへ残さない。"""
    process = mocker.patch(
        "pyfltr.command.process.run_subprocess",
        side_effect=[subprocess.CompletedProcess([], 2, _report(503)), subprocess.CompletedProcess([], 0, _report())],
    )
    assert _run_cli(lychee_project) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    command = next(record for record in records if record["kind"] == "command")
    assert command["status"] == "succeeded"
    header = next(record for record in records if record["kind"] == "header")
    store = pyfltr.state.archive.ArchiveStore(cache_root=lychee_project / "cache")
    assert not store.read_tool_diagnostics(header["run_id"], "lychee")
    assert "503" not in store.read_tool_output(header["run_id"], "lychee")
    assert process.call_count == 2


@pytest.mark.parametrize("code, expected_status, expected_exit", [(503, "warning", 0), (404, "failed", 1)])
def test_lychee_cli_text(
    lychee_project: pathlib.Path,
    mocker: typing.Any,
    capsys: pytest.CaptureFixture[str],
    code: int,
    expected_status: str,
    expected_exit: int,
) -> None:
    """text利用者にもURL・原因と警告または失敗が届く。"""
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess([], 2, _report(code)))
    assert _run_cli(lychee_project, output_format="text") == expected_exit
    output = capsys.readouterr().out
    assert expected_status in output
    assert "https://example.com/link-0" in output
    assert str(code) in output


@pytest.mark.asyncio
@pytest.mark.parametrize("code, expected_status, expected_exit", [(503, "warning", 0), (404, "failed", 1)])
async def test_lychee_mcp_classification(
    lychee_project: pathlib.Path,
    mocker: typing.Any,
    code: int,
    expected_status: str,
    expected_exit: int,
) -> None:
    """MCPの公開runも共通実行経路の成否と診断を返す。"""
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess([], 2, _report(code)))
    result = await pyfltr.cli.mcp_server.tool_run(
        paths=[str(lychee_project)], commands=["lychee"], mode="ci", work_dir=str(lychee_project), no_cache=True
    )
    assert result.exit_code == expected_exit
    assert result.commands[0].status == expected_status
    assert result.failed == (["lychee"] if expected_exit else [])
    assert result.commands[0].diagnostics == 1


@pytest.mark.parametrize(
    "output, returncode",
    [
        ("not json", 2),
        ("{}", 2),
        (_report(503).replace('"timeouts": 0,', ""), 2),
        (_report(503).replace('"errors": 1', '"errors": 2'), 2),
        (_report(503).replace('"unknown": 0', '"unknown": 1'), 2),
        (_report(503).replace('"url": "https:', '"url": "file:'), 2),
        (_report(503) + "\nprocess failed", 2),
        (_report(503), 127),
        (_report(503), -9),
    ],
)
def test_lychee_cli_unclassified_failure(
    lychee_project: pathlib.Path,
    mocker: typing.Any,
    capsys: pytest.CaptureFixture[str],
    output: str,
    returncode: int,
) -> None:
    """不完全な結果や起動異常を一時障害の成功へ格下げしない。"""
    process = mocker.patch(
        "pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess([], returncode, output)
    )
    assert _run_cli(lychee_project) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(record for record in records if record["kind"] == "command")["status"] == "failed"
    assert process.call_count == (2 if returncode == -9 else 1)


def test_lychee_execution_timeout(lychee_project: pathlib.Path, mocker: typing.Any, capsys: pytest.CaptureFixture[str]) -> None:
    """プロセス上限への到達は有効な5xx JSONが残っていても失敗になる。"""
    mocker.patch(
        "pyfltr.command.process.run_subprocess",
        side_effect=pyfltr.command.process.TimeoutExceededExecution(output=_report(503), elapsed=1.0, timeout=1.0),
    )
    assert _run_cli(lychee_project) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(record for record in records if record["kind"] == "command")["status"] == "failed"


def test_lychee_retry_wait_consumes_timeout(
    lychee_project: pathlib.Path, mocker: typing.Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """再試行待機が時間上限へ達した場合も警告へ格下げしない。"""
    with (lychee_project / "pyproject.toml").open("a", encoding="utf-8") as config_file:
        config_file.write("lychee-timeout = 1\n")
    process = mocker.patch(
        "pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess([], 2, _report(503))
    )
    assert _run_cli(lychee_project) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(record for record in records if record["kind"] == "command")["status"] == "failed"
    assert process.call_count == 1


def test_lychee_explicit_warning_keeps_404_warning(
    lychee_project: pathlib.Path, mocker: typing.Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """利用者が明示した一律警告の指定を維持する。"""
    with (lychee_project / "pyproject.toml").open("a", encoding="utf-8") as config_file:
        config_file.write('lychee-severity = "warning"\n')
    mocker.patch("pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess([], 2, _report(404)))
    assert _run_cli(lychee_project) == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(record for record in records if record["kind"] == "command")["status"] == "warning"


@pytest.mark.parametrize("first_code, second_code", [(503, 404), (404, 503)])
def test_lychee_subprojects_keep_failure(
    lychee_project: pathlib.Path,
    mocker: typing.Any,
    capsys: pytest.CaptureFixture[str],
    first_code: int,
    second_code: int,
) -> None:
    """警告と失敗が別サブプロジェクトにあっても集約順序で失敗が消えない。"""
    for name in ("a", "b"):
        child = lychee_project / name
        child.mkdir()
        (child / "pyproject.toml").write_text((lychee_project / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
        (child / "links.md").write_text("[link](https://example.com)\n", encoding="utf-8")

    def _run(
        commandline: list[str], *_args: typing.Any, cwd: pathlib.Path, **_kwargs: typing.Any
    ) -> subprocess.CompletedProcess[str]:
        code = first_code if cwd.name == "a" else second_code
        return subprocess.CompletedProcess(commandline, 2, _report(code))

    mocker.patch("pyfltr.command.process.run_subprocess", side_effect=_run)
    assert _run_cli(lychee_project) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(record for record in records if record["kind"] == "command")["status"] == "failed"


def test_lychee_negative_oom_retries_still_runs(
    lychee_project: pathlib.Path, mocker: typing.Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """OOM再試行無効を表す負値でも初回のリンクチェックを実行する。"""
    with (lychee_project / "pyproject.toml").open("a", encoding="utf-8") as config_file:
        config_file.write("retry-max-attempts = -1\n")
    process = mocker.patch(
        "pyfltr.command.process.run_subprocess", return_value=subprocess.CompletedProcess([], 2, _report(404))
    )
    assert _run_cli(lychee_project) == 1
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert next(record for record in records if record["kind"] == "command")["status"] == "failed"
    assert process.call_count == 1
