"""`list-runs` / `show-run` サブコマンドのテスト。

`PYFLTR_CACHE_DIR` を `tmp_path` に固定することで、テストデータ生成に使う
`ArchiveStore(cache_root=tmp_path)` と `pyfltr.cli.main.run([...])` 経由で
生成される `ArchiveStore()`（`default_cache_root()` 解決）が同一キャッシュを参照する。
"""

import json
import pathlib

import pytest

import pyfltr.cli.main
import pyfltr.command.error_parser
import pyfltr.state.archive
from tests.conftest import make_error_location as _make_error
from tests.conftest import seed_archive_run as _seed_run


@pytest.fixture(autouse=True)
def _isolated_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> pathlib.Path:
    """`PYFLTR_CACHE_DIR`を`tmp_path`に固定するフィクスチャ。"""
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(tmp_path))
    return tmp_path


def test_list_runs_text_empty(capsys: pytest.CaptureFixture[str]) -> None:
    returncode = pyfltr.cli.main.run(["list-runs"])
    assert returncode == 0
    captured = capsys.readouterr()
    assert "(no runs)" in captured.out


def test_list_runs_text_multiple(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id1 = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)
    run_id2 = _seed_run(tmp_path, commands=["mypy"], exit_code=1)

    returncode = pyfltr.cli.main.run(["list-runs"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "RUN_ID" in out
    # 新しい順（降順）で並ぶ
    idx1 = out.find(run_id1)
    idx2 = out.find(run_id2)
    assert idx1 >= 0 and idx2 >= 0
    assert idx2 < idx1  # 新しい run_id2 (後から作成) が先


def test_list_runs_limit(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for _ in range(5):
        _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["list-runs", "--limit", "2"])
    assert returncode == 0
    out = capsys.readouterr().out
    # header行 + 2件 = 3行
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 3


def test_list_runs_json(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path, commands=["ruff-check"], files=2, exit_code=0)

    returncode = pyfltr.cli.main.run(["list-runs", "--output-format=json"])
    assert returncode == 0
    payload = json.loads(capsys.readouterr().out)
    assert "runs" in payload
    assert payload["runs"][0]["run_id"] == run_id
    assert payload["runs"][0]["commands"] == ["ruff-check"]
    assert payload["runs"][0]["files"] == 2


def test_list_runs_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id1 = _seed_run(tmp_path)
    run_id2 = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["list-runs", "--output-format=jsonl"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    assert all(line["kind"] == "run" for line in lines)
    # 降順（新しい順）
    assert lines[0]["run_id"] == run_id2
    assert lines[1]["run_id"] == run_id1


def test_show_run_text_overview(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        commands=["ruff-check", "mypy"],
        tool_results=[
            ("ruff-check", 0, "clean", []),
            ("mypy", 1, "error", [_make_error("mypy", "a.py", 1, "boom")]),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id])
    assert returncode == 0
    out = capsys.readouterr().out
    assert f"run_id: {run_id}" in out
    assert "commands:" in out
    assert "ruff-check:" in out
    assert "mypy:" in out
    assert "diagnostics=1" in out


def test_show_run_prefix(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", run_id[:8]])
    assert returncode == 0
    assert f"run_id: {run_id}" in capsys.readouterr().out


def test_show_run_latest(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_run(tmp_path)
    latest_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", "latest"])
    assert returncode == 0
    assert f"run_id: {latest_id}" in capsys.readouterr().out


def test_show_run_prefix_ambiguous(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_ids = [_seed_run(tmp_path) for _ in range(2)]
    # ULIDの先頭は同じタイムスタンプ部分（ミリ秒単位）を共有する可能性が高いため、
    # 実際に共通する最長プレフィックスを算出してテストする。
    shared = 0
    for a, b in zip(run_ids[0], run_ids[1], strict=False):
        if a != b:
            break
        shared += 1
    if shared < 1:
        pytest.skip("shared prefixが無いケースでは曖昧判定にならない")
    prefix = run_ids[0][:shared]

    returncode = pyfltr.cli.main.run(["show-run", prefix])
    assert returncode == 1
    assert "曖昧" in capsys.readouterr().err


def test_show_run_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    returncode = pyfltr.cli.main.run(["show-run", "nonexistent"])
    assert returncode == 1
    assert "run_id" in capsys.readouterr().err


def test_show_run_latest_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    returncode = pyfltr.cli.main.run(["show-run", "latest"])
    assert returncode == 1
    assert "run" in capsys.readouterr().err


def test_list_runs_ai_agent_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI_AGENT 設定時、`list-runs` は --output-format 未指定でも JSONL を出力する。"""
    monkeypatch.setenv("AI_AGENT", "1")
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["list-runs"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "run"
    assert lines[0]["run_id"] == run_id


def test_show_run_ai_agent_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI_AGENT 設定時、`show-run` は --output-format 未指定でも JSONL を出力する。"""
    monkeypatch.setenv("AI_AGENT", "1")
    run_id = _seed_run(tmp_path, commands=["ruff-check"], exit_code=0)

    returncode = pyfltr.cli.main.run(["show-run", run_id])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    # JSONL: meta（kind="meta"）+ ツール別command
    assert lines, "JSONLが1行も出ていない"
    assert lines[0]["kind"] == "meta"


def test_show_run_tool_text(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            (
                "mypy",
                1,
                "mypy output",
                [_make_error("mypy", "src/a.py", 42, "型エラー", col=5)],
            ),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "mypy"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "command: mypy" in out
    assert "src/a.py:42:5" in out
    assert "型エラー" in out


def test_show_run_tool_text_renders_hint_urls_and_hints(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`hint_urls`と`hints`がtool.jsonにあるとき`show-run --commands`のtext出力に表示される。"""
    error = pyfltr.command.error_parser.ErrorLocation(
        file="src/a.py",
        line=1,
        col=2,
        command="ruff-check",
        message="unused import",
        rule="F401",
        rule_url="https://docs.astral.sh/ruff/rules/F401/",
        hint="Remove the unused import.",
    )
    run_id = _seed_run(
        tmp_path,
        tool_results=[("ruff-check", 1, "out", [error])],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "hint_urls:" in out
    assert "F401: https://docs.astral.sh/ruff/rules/F401/" in out
    assert "hints:" in out
    assert "F401: Remove the unused import." in out


def test_show_run_tool_json(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            (
                "ruff-check",
                1,
                "out",
                [_make_error("ruff-check", "a.py", 1, "msg")],
            ),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check", "--output-format=json"])
    assert returncode == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"]["command"] == "ruff-check"
    assert len(payload["diagnostics"]) == 1
    assert payload["diagnostics"][0]["file"] == "a.py"


def test_show_run_tool_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            (
                "ruff-check",
                1,
                "out",
                [
                    _make_error("ruff-check", "a.py", 1, "msg-a"),
                    _make_error("ruff-check", "b.py", 2, "msg-b"),
                ],
            ),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check", "--output-format=jsonl"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["kind"] == "command"
    assert lines[0]["command"] == "ruff-check"
    assert [line["kind"] for line in lines[1:]] == ["diagnostic", "diagnostic"]
    assert lines[1]["file"] == "a.py"


def test_show_run_tool_not_found(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "nonexistent"])
    assert returncode == 1
    assert "nonexistent" in capsys.readouterr().err


def test_show_run_output_mode(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 1, "raw output line 1\nraw output line 2\n", []),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check", "--output"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "raw output line 1" in out
    assert "raw output line 2" in out


def test_show_run_output_mode_jsonl(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[("ruff-check", 0, "raw-log", [])],
    )

    returncode = pyfltr.cli.main.run(
        [
            "show-run",
            run_id,
            "--commands",
            "ruff-check",
            "--output",
            "--output-format=jsonl",
        ]
    )
    assert returncode == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["kind"] == "output"
    assert record["command"] == "ruff-check"
    assert record["content"] == "raw-log"


def test_show_run_output_without_tool_errors(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(tmp_path)

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--output"])
    assert returncode == 1
    assert "--commands" in capsys.readouterr().err


def test_show_run_commands_multiple(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 1, "out-r", [_make_error("ruff-check", "a.py", 1, "msg-a")]),
            ("mypy", 1, "out-m", [_make_error("mypy", "b.py", 2, "msg-b")]),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check,mypy"])
    assert returncode == 0
    out = capsys.readouterr().out
    assert "command: ruff-check" in out
    assert "command: mypy" in out
    assert "msg-a" in out
    assert "msg-b" in out


def test_show_run_output_with_multiple_commands_errors(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "", []),
            ("mypy", 0, "", []),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--commands", "ruff-check,mypy", "--output"])
    assert returncode == 1
    assert "単一" in capsys.readouterr().err


def test_show_run_jsonl_overview(
    tmp_path: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = _seed_run(
        tmp_path,
        tool_results=[
            ("ruff-check", 0, "", []),
            ("mypy", 0, "", []),
        ],
    )

    returncode = pyfltr.cli.main.run(["show-run", run_id, "--output-format=jsonl"])
    assert returncode == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    kinds = [line["kind"] for line in lines]
    assert kinds[0] == "meta"
    assert kinds[1:] == ["command", "command"]
    assert lines[0]["run_id"] == run_id
