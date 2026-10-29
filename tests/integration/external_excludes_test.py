"""外部パスの除外範囲をCLIとMCPから検証する。"""

import json
import pathlib
import sys

import pytest
import tomlkit

import pyfltr.cli.main
import pyfltr.cli.mcp_server


@pytest.mark.parametrize("exclude_key", ["exclude", "extend-exclude", "probe-exclude"])
@pytest.mark.parametrize("target_kind", ["file", "directory", "overlapping"])
def test_cli_external_exclude_boundary(tmp_path: pathlib.Path, exclude_key: str, target_kind: str) -> None:
    """指定引数より上の除外名を無視し、指定名と配下の除外を維持する。"""
    project = tmp_path / "project"
    project.mkdir()
    selected = tmp_path / ".cache" / "selected"
    selected.mkdir(parents=True)
    kept = selected / "sample.txt"
    kept.write_text("needle\n", encoding="utf-8")
    hidden = selected / ".venv" / "hidden.txt"
    hidden.parent.mkdir()
    hidden.write_text("needle\n", encoding="utf-8")
    minified = selected / "sample.min.js"
    minified.write_text("needle\n", encoding="utf-8")
    output = project / "received.json"
    script = project / "probe.py"
    script.write_text(
        "import json, pathlib, sys\npathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]), encoding='utf-8')\n",
        encoding="utf-8",
    )
    settings = {
        "exclude": [],
        "extend-exclude": [],
        "respect-gitignore": False,
        exclude_key: [".cache", ".venv", "*.min.js"],
        "custom-commands": {
            "probe": {"type": "linter", "path": sys.executable, "args": [str(script), str(output)], "targets": "*"}
        },
    }
    (project / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"pyfltr": settings}}), encoding="utf-8")
    targets = [kept] if target_kind == "file" else [selected]
    if target_kind == "overlapping":
        targets.append(hidden)

    assert (
        pyfltr.cli.main.run(
            [
                "run",
                "--work-dir",
                str(project),
                "--commands=probe",
                "--allow-external-paths",
                "--no-fix",
                "--no-cache",
                "--no-archive",
                *map(str, targets),
            ]
        )
        == 0
    )

    received = json.loads(output.read_text(encoding="utf-8"))
    expected = [kept, hidden] if target_kind == "overlapping" else [kept]
    assert sorted(received) == sorted(map(str, expected))


@pytest.mark.parametrize("as_directory", [False, True])
@pytest.mark.asyncio
async def test_mcp_grep_external_exclude_boundary(tmp_path: pathlib.Path, as_directory: bool) -> None:
    """外部検索で上位.cacheを照合せず、指定配下の既定除外を維持する。"""
    selected = tmp_path / ".cache" / "selected"
    selected.mkdir(parents=True)
    kept = selected / "sample.txt"
    kept.write_text("needle\n", encoding="utf-8")
    hidden = selected / ".venv" / "hidden.txt"
    hidden.parent.mkdir()
    hidden.write_text("needle\n", encoding="utf-8")
    (selected / "sample.min.js").write_text("needle\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(paths=[str(selected if as_directory else kept)], pattern="needle")

    assert result.exit_code == 0
    assert result.files_scanned == 1
    assert [match.file for match in result.matches] == [kept.as_posix()]


@pytest.mark.parametrize("name", [".venv", "sample.min.js"])
@pytest.mark.asyncio
async def test_mcp_grep_excludes_external_argument_name(tmp_path: pathlib.Path, name: str) -> None:
    """外部指定自体の名前に一致する除外は検索にも適用する。"""
    target = tmp_path / name
    if name == ".venv":
        target.mkdir()
        (target / "sample.txt").write_text("needle\n", encoding="utf-8")
    else:
        target.write_text("needle\n", encoding="utf-8")

    result = await pyfltr.cli.mcp_server.tool_grep(paths=[str(target)], pattern="needle")

    assert result.exit_code == 1
    assert result.files_scanned == 0
    assert not result.matches
