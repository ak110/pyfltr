import pathlib
import subprocess

import pytest
import yaml

import pyfltr.verification_sync


@pytest.fixture(name="repository")
def repository_fixture(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    monkeypatch.chdir(tmp_path)
    _git("init")
    skill = tmp_path / ".claude" / "skills" / "example" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"---\nname: example\nverifies:\n  - 'src/**/*.txt'\n---\n# Example\n")
    target = tmp_path / "src" / "example.txt"
    target.parent.mkdir()
    target.write_bytes(b"one\n")
    _git("add", ".")
    return tmp_path, skill, target


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.mark.parametrize("staged", [False, True])
def test_verification_sync_changes(repository: tuple[pathlib.Path, pathlib.Path, pathlib.Path], staged: bool) -> None:
    root, skill, target = repository
    diagnostics = pyfltr.verification_sync.check(root, staged=staged)
    assert len(diagnostics) == 1
    assert "verified-digest" in diagnostics[0]
    pyfltr.verification_sync.update(skill)
    _git("add", ".")
    assert not pyfltr.verification_sync.check(root, staged=staged)
    target.write_bytes(b"one\r\n")
    _git("add", ".")
    assert not pyfltr.verification_sync.check(root, staged=staged)
    target.write_bytes(b"two\n")
    _git("add", ".")
    diagnostics = pyfltr.verification_sync.check(root, staged=staged)
    assert len(diagnostics) == 1
    assert "src/example.txt" in diagnostics[0]
    assert ".claude/skills/example/SKILL.md" in diagnostics[0]
    pyfltr.verification_sync.update(skill)
    _git("add", ".")
    assert not pyfltr.verification_sync.check(root, staged=staged)
    (root / "unrelated.txt").write_bytes(b"unrelated\n")
    _git("add", ".")
    assert not pyfltr.verification_sync.check(root, staged=staged)


def test_verification_sync_checks_partial_stage(repository: tuple[pathlib.Path, pathlib.Path, pathlib.Path]) -> None:
    root, skill, target = repository
    target.write_bytes(b"two\n")
    _git("add", ".")
    target.write_bytes(b"three\n")
    pyfltr.verification_sync.update(skill)
    _git("add", str(skill))
    assert not pyfltr.verification_sync.check(root, staged=False)
    assert pyfltr.verification_sync.check(root, staged=True)


@pytest.mark.parametrize("staged", [False, True])
def test_verification_sync_checks_empty_match(
    repository: tuple[pathlib.Path, pathlib.Path, pathlib.Path], staged: bool
) -> None:
    root, skill, _target = repository
    skill.write_bytes(b"---\nverifies: ['nonexistent/*']\nverified-digest: missing\n---\n")
    _git("add", ".")
    assert "一致する追跡ファイルがありません" in pyfltr.verification_sync.check(root, staged=staged)[0]


@pytest.mark.parametrize(
    "other_keys",
    [
        b"verified-digest: &saved old\nother: *saved\n",
        b"verified-digest: old\nverified-digest: old\n",
    ],
)
def test_verification_sync_update_preserves_unrelated_yaml(
    repository: tuple[pathlib.Path, pathlib.Path, pathlib.Path], other_keys: bytes
) -> None:
    _root, skill, _target = repository
    original = b"---\nverifies: [src/example.txt]\n" + other_keys + b"---\n"
    skill.write_bytes(original)
    with pytest.raises((pyfltr.verification_sync.VerificationSyncError, yaml.YAMLError)):
        pyfltr.verification_sync.update(skill)
    assert skill.read_bytes() == original
