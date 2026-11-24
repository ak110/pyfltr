from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_IMAGE = "ghcr.io/ak110/pyfltr"
_TAG = "v3.35.1"
_SOURCE_SHA = "ab" * 20
_BASH = shutil.which("bash")
_TEST_ENV = pytest.mark.skipif(sys.platform == "win32" or not _BASH, reason="Ubuntu runnerのBashを使う")


def _workflow_job(workflow: str, job: str) -> dict[str, Any]:
    source = (_ROOT / ".github" / "workflows" / f"{workflow}.yaml").read_text(encoding="utf-8")
    jobs: dict[str, dict[str, Any]] = yaml.load(source, Loader=yaml.BaseLoader)["jobs"]
    return jobs[job]


def _bash(script: str, tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    assert _BASH is not None
    return subprocess.run(
        [_BASH, "-c", script],
        cwd=tmp_path,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=10,
    )


@_TEST_ENV
@pytest.mark.parametrize(
    ("protect_latest", "newest_tag", "update_latest"),
    [
        pytest.param("true", _TAG, True, id="current-release"),
        pytest.param("true", "v3.35.2", False, id="older-release-rerun"),
        pytest.param("", "v3.35.2", True, id="manual-rebuild"),
    ],
)
def test_docker_push_tags(tmp_path: Path, protect_latest: str, newest_tag: str, *, update_latest: bool) -> None:
    steps = _workflow_job("docker-build", "build")["steps"]
    output = tmp_path / "output"
    select_tags = next((s for s in steps if s.get("id") == "tags"), None)
    output_values = {"tag": _TAG}
    if select_tags is not None:
        result = _bash(
            r"""
git() {
  [[ "$*" == "ls-remote --tags --refs --sort=-v:refname origin v*" ]] || return 2
  printf '123\trefs/tags/v3.99.0-rc1\n123\trefs/tags/%s\n' "$NEWEST_TAG"
}
"""
            + select_tags["run"],
            tmp_path,
            TAG=_TAG,
            NEWEST_TAG=newest_tag,
            PROTECT_LATEST=protect_latest,
            GITHUB_OUTPUT=str(output),
        )
        assert result.returncode == 0, result.stderr
        output_values["list"] = output.read_text(encoding="utf-8").split("list<<EOF\n")[1].rsplit("\nEOF", 1)[0]

    builds = [s for s in steps if s.get("with", {}).get("push") == "true"]
    assert len(builds) == 2
    expected = [f"{_IMAGE}:{_TAG}"]
    if update_latest:
        expected.append(f"{_IMAGE}:latest")
    for build in builds:
        tags = build["with"]["tags"].replace("${{ steps.version.outputs.tag }}", output_values["tag"])
        tags = tags.replace("${{ steps.tags.outputs.list }}", output_values.get("list", ""))
        assert tags.splitlines() == expected


def _release_notes_script() -> str:
    return next(
        step["run"]
        for step in _workflow_job("release", "release")["steps"]
        if step.get("name") == "Publish GitHub Release with verification instructions"
    )


_MOCK_RELEASE = r"""
gh() {
  case "$1 $2" in
    'release view')
      [[ "$EXISTS" == yes ]] || return 1
      printf '%s\n' "$BODY"
      ;;
    'release edit'|'release create') printf '%s\n' "$2";;
    *) return 2;;
  esac
}
"""


@_TEST_ENV
@pytest.mark.parametrize("existing", [None, "# Original", "# Original\n\n## コンテナイメージの来歴証明\n\n準備中。"])
def test_release_notes_fill_missing_verification(tmp_path: Path, existing: str | None) -> None:
    notes = tmp_path / "CHANGES.md"
    changelog = "# Changelog\n"
    notes.write_text(changelog, encoding="utf-8")
    script = _MOCK_RELEASE + _release_notes_script()

    result = _bash(script, tmp_path, TAG=_TAG, EXISTS="yes" if existing is not None else "no", BODY=existing or "")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("edit" if existing is not None else "create")
    body = notes.read_text(encoding="utf-8")
    assert body.startswith(existing if existing is not None else changelog)
    assert "https://github.com/ak110/pyfltr/attestations" in body

    repeated = _bash(script, tmp_path, TAG=_TAG, EXISTS="yes", BODY=body)
    assert repeated.returncode == 0, repeated.stderr
    assert repeated.stdout == ""
    assert notes.read_text(encoding="utf-8") == body


@_TEST_ENV
@pytest.mark.parametrize("entry", ["guide", "release"])
def test_published_verification_checks_the_tag_commit(tmp_path: Path, entry: str) -> None:
    if entry == "guide":
        body = (_ROOT / "docs" / "guide" / "getting-started.md").read_text(encoding="utf-8")
    else:
        (tmp_path / "CHANGES.md").write_text("", encoding="utf-8")
        source = _bash(_MOCK_RELEASE + _release_notes_script(), tmp_path, TAG=_TAG, EXISTS="no")
        assert source.returncode == 0, source.stderr
        body = (tmp_path / "CHANGES.md").read_text(encoding="utf-8")

    command = next(block for block in re.findall(r"```shell\n(.*?)```", body, re.S) if "gh attestation verify" in block)
    result = _bash(
        r"""
gh() {
  if [[ "$1" == api ]]; then
    [[ "$2" == "repos/ak110/pyfltr/commits/v3.35.1" ]] || return 2
    printf '%s\n' "$EXPECTED_SHA"
  else
    printf '%s\n' "$*"
  fi
}
"""
        + command.replace("vX.Y.Z", _TAG),
        tmp_path,
        EXPECTED_SHA=_SOURCE_SHA,
    )
    assert result.returncode == 0, result.stderr
    assert (
        result.stdout.strip() == f"attestation verify oci://{_IMAGE}:{_TAG} --repo ak110/pyfltr "
        f"--signer-workflow ak110/pyfltr/.github/workflows/docker-build.yaml --source-digest {_SOURCE_SHA}"
    )


@_TEST_ENV
def test_untagged_commit_before_latest_release_cannot_allocate_a_version(tmp_path: Path) -> None:
    step = next(s for s in _workflow_job("release", "release")["steps"] if s.get("id") == "next")
    script = step["run"].replace("${{ github.event.inputs.bump }}", "PATCH")
    output = tmp_path / "output"
    result = _bash(
        r"""
git() {
  case "$1" in
    tag)
      if [[ "$2" != --points-at=HEAD ]]; then printf 'v3.35.2\n'; fi
      ;;
    merge-base) return 1;;
    *) return 2;;
  esac
}
"""
        + script,
        tmp_path,
        BUMP="PATCH",
        GITHUB_OUTPUT=str(output),
    )
    assert result.returncode != 0
    assert not output.exists()
