import json
import pathlib
import shlex
import subprocess

import pytest

import pyfltr.cli.main
import pyfltr.cli.output_format
import pyfltr.command.only_failed
import pyfltr.output.logging_
import pyfltr.run_options
import pyfltr.state.archive
import pyfltr.state.only_failed
import pyfltr.warnings_


def test_only_failed_early_exit_emits_jsonl_summary_with_reason(
    _only_failed_cache: pathlib.Path,
    _isolated_target: pathlib.Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """直前runが無い状態の`--only-failed`は、JSONLでheader・理由付きwarning・summaryを出力して0で終わる。

    理由をINFOでしか出力しないと、WARN以上だけを出力するJSONL経路では何も出力されず、
    エージェントはスキップの理由も次の操作も受け取れない。
    """
    returncode = pyfltr.cli.main.run(
        [
            "run",
            "--only-failed",
            "--output-format=jsonl",
            "--work-dir",
            str(_isolated_target),
            str(_isolated_target / "sample.py"),
        ]
    )

    assert returncode == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    kinds = [record["kind"] for record in records]
    assert kinds[0] == "header"
    assert kinds[-1] == "summary"
    only_failed_warnings = [r for r in records if r["kind"] == "warning" and r["source"] == "only-failed"]
    assert len(only_failed_warnings) == 1
    assert "直前の run が無い" in only_failed_warnings[0]["msg"]
    assert "--only-failed を外して" in only_failed_warnings[0]["hint"]
    assert records[-1]["exit"] == 0
    assert records[-1]["warnings"] == kinds.count("warning")


@pytest.mark.parametrize(
    ("target", "entity"),
    [
        # `..`を含む起点内のパス
        ("sub/../a.py", "proj/a.py"),
        # 起点外を指す相対パス
        ("../outside/out.py", "outside/out.py"),
    ],
)
def test_retry_command_and_only_failed_keep_target_written_with_dot_dot(
    tmp_path: pathlib.Path,
    mocker,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    entity: str,
) -> None:
    """`..`を含む対象で失敗した後も、`retry_command`が対象を持ち、`--only-failed`が同じファイルを再実行する。

    診断の`file`は起点相対・起点外の絶対パスへ正規化され、対象は利用者が渡した表現を保つ。
    両者を文字列で比べると`retry_command`は対象を持たず、`--only-failed`は実行を省いて終了コード0で終わる。
    """
    work_dir = tmp_path / "proj"
    (work_dir / "sub").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (work_dir / "pyproject.toml").write_text('[tool.pyfltr]\nmypy = true\nmypy-runner = "direct"\n', encoding="utf-8")
    (tmp_path / entity).write_text("x = 1\n", encoding="utf-8")
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("PYFLTR_CACHE_DIR", str(cache_root))
    monkeypatch.chdir(work_dir)

    def fake_run(commandline, *args, **kwargs):
        del args, kwargs  # 未使用
        files = [arg for arg in commandline if str(arg).endswith(".py")]
        stdout = "".join(f"{file}:1: error: 型が違う  [assignment]\n" for file in files)
        return subprocess.CompletedProcess(commandline, returncode=1 if files else 0, stdout=stdout)

    mock_run = mocker.patch("pyfltr.command.process.run_subprocess", side_effect=fake_run)
    base_args = ["run", "--work-dir", str(work_dir), "--commands=mypy", "--no-cache", "--no-gitignore"]
    assert pyfltr.cli.main.run([*base_args, target]) == 1

    store = pyfltr.state.archive.ArchiveStore(cache_root=cache_root)
    run_id = store.list_runs(limit=1)[0].run_id
    retry_command = store.read_tool_meta(run_id, "mypy")["retry_command"]
    assert shlex.split(retry_command)[-1] == str((tmp_path / entity).resolve())

    mock_run.reset_mock()
    assert pyfltr.cli.main.run([*base_args, "--only-failed", target]) == 1
    assert not [w for w in pyfltr.warnings_.collected_warnings() if w["source"] == "only-failed"]
    mypy_calls = [call for call in mock_run.call_args_list if "mypy" in " ".join(call.args[0])]
    assert len(mypy_calls) == 1
