import json
import pathlib

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
