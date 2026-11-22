import json
import pathlib
import subprocess

import pytest

from pyfltr.command import pytest_impact


def test_select_files_preserves_scope_and_mandatory_targets(tmp_path: pathlib.Path) -> None:
    cwd = tmp_path / "component"
    scope = [
        pathlib.Path("component") / file
        for file in ("affected_test.py", "always_test.py", "new_test.py", "skip_test.py", "nested/path_test.py")
    ]
    result = pytest_impact.Probe(
        tests=("affected_test.py::test_func", "excluded_test.py::test_func"),
        paths=("nested",),
        known=("affected_test.py::test_func", "always_test.py::test_func", "skip_test.py::test_func"),
    )
    selected = pytest_impact.select_files(scope, [scope[1]], result, start_cwd=tmp_path, cwd=cwd)
    assert selected == [scope[0], scope[1], scope[2], scope[4]]


@pytest.mark.parametrize("accept", [False, True])
def test_recording_preserves_previous_map_on_failure(tmp_path: pathlib.Path, *, accept: bool) -> None:
    directory = tmp_path / ".testscope"
    directory.mkdir()
    old_map = directory / "map.json"
    old_map.write_text("old", encoding="utf-8")
    lastfailed = directory / "lastfailed.json"
    with pytest_impact.recording(tmp_path, "current", [pathlib.Path("first_test.py")], tmp_path) as active:
        assert active is not None
        old_map.write_text(json.dumps({"tests": {"first_test.py::test_example": []}}), encoding="utf-8")
        lastfailed.write_text("[]", encoding="utf-8")
        if accept:
            active.accept()
        assert active.complete is accept
    assert (old_map.read_text(encoding="utf-8") != "old") is accept
    assert lastfailed.exists() is accept
    assert (directory / "pyfltr.json").exists() is accept


@pytest.mark.parametrize(
    "stdout",
    [b"not json", b'{"tests": [1]}', b'{"signature":"environment"}', b'{"reason":null}'],
)
def test_invalid_probe_response_falls_back(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout))
    result = pytest_impact.probe(["uv", "run", "--frozen", "pytest"], "uv", tmp_path, {}, [], record=False)
    assert result.reason
    assert not result.known


def test_recording_rejects_partial_tests_in_unchanged_file(tmp_path: pathlib.Path) -> None:
    file = pathlib.Path("first_test.py")
    directory = tmp_path / ".testscope"
    directory.mkdir()
    path = directory / "map.json"
    nodes: dict[str, list[int]] = {"first_test.py::test_first": [], "first_test.py::test_second": []}
    snap = {"first_test.py": {"sha": "same"}}
    baseline = json.dumps({"tests": nodes, "snap": snap})
    path.write_text(baseline, encoding="utf-8")

    with pytest_impact.recording(tmp_path, "current", [file], tmp_path) as active:
        assert active is not None
        del nodes["first_test.py::test_second"]
        path.write_text(json.dumps({"tests": nodes, "snap": snap}), encoding="utf-8")
        active.accept()
        assert not active.complete

    assert path.read_text(encoding="utf-8") == baseline
