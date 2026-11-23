import hashlib
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import typing

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


@pytest.mark.parametrize("sep", ["\\", "/"])
def test_posix_deps_aligns_recorded_paths_with_git_paths(*, sep: str) -> None:
    namespace: dict[str, typing.Any] = {}
    exec(pytest_impact._POSIX_DEPS, namespace)  # noqa: S102  # pylint: disable=exec-used,protected-access
    deps = [f"f:src{sep}pkg{sep}mod.py::func", f"r:data{sep}a.txt", f"d:data{sep}dir", "p:requests"]
    assert namespace["posix_deps"](deps, sep) == [
        "f:src/pkg/mod.py::func",
        "r:data/a.txt",
        "d:data/dir",
        "p:requests",
    ]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="選択処理はPython 3.12以上だけで動く")
def test_probe_selects_tests_from_map_recorded_with_os_separator(tmp_path: pathlib.Path) -> None:
    site = tmp_path / "site"
    package = site / "testscope"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "selection.py").write_text(
        textwrap.dedent(
            """
            import types

            MAP_VERSION = 2

            def compute(root, m):
                changed = ["src/pkg/mod.py"]
                deps = {d: i for i, d in enumerate(m["deps"])}
                hit = {deps[f"f:{f}::func"] for f in changed if f"f:{f}::func" in deps}
                tests = {n: "changed" for n, ids in m["tests"].items() if hit & set(ids)}
                return types.SimpleNamespace(full=None, unobserved=[], changed=changed, tests=tests, paths={})
            """
        ),
        encoding="utf-8",
    )
    dist_info = site / "testscope-0.4.2.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Metadata-Version: 2.1\nName: testscope\nVersion: 0.4.2\n", encoding="utf-8")
    project = tmp_path / "project"
    (project / ".testscope").mkdir(parents=True)
    env = {**os.environ, "PYTHONPATH": str(site)}
    command = [sys.executable, "-c", pytest_impact._PROBE]  # pylint: disable=protected-access
    record = subprocess.run([*command, "record"], cwd=project, env=env, capture_output=True, check=True)
    signature = json.loads(record.stdout)["signature"]
    mapping = {
        "version": 2,
        "python": sys.version.split()[0],
        "deps": [f"f:src{os.sep}pkg{os.sep}mod.py::func", f"f:src{os.sep}pkg{os.sep}other.py::func"],
        "tests": {"tests/mod_test.py::test_mod": [0], "tests/other_test.py::test_other": [1]},
    }
    data = json.dumps(mapping).encode()
    (project / ".testscope" / "map.json").write_bytes(data)
    (project / ".testscope" / "pyfltr.json").write_text(
        json.dumps([signature, hashlib.sha256(data).hexdigest()]), encoding="utf-8"
    )
    selected = subprocess.run([*command, "select"], cwd=project, env=env, capture_output=True, check=True)
    assert json.loads(selected.stdout)["tests"] == ["tests/mod_test.py::test_mod"]
