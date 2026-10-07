import pathlib
import re

import pytest
import yaml

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("path", "excluded"),
    [
        (".agents/skills", True),
        ("sample/.agents/skills", True),
        (".agents/skills-extra", False),
        (".agents/skills/file", False),
        (".agents/other", False),
    ],
)
def test_end_of_file_fixer_excludes_git_symlink(path: str, excluded: bool) -> None:
    """通常ファイル化するGit symlinkだけを末尾改行修正から除外する。"""
    config = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(hook for repository in config["repos"] for hook in repository["hooks"] if hook["id"] == "end-of-file-fixer")

    assert bool(re.search(hook["exclude"], path)) is excluded
