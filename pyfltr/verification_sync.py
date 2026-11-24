from __future__ import annotations

import fnmatch
import hashlib
import pathlib
import subprocess

import yaml


class VerificationSyncError(ValueError):
    pass


class RepositoryFiles:
    def __init__(self, cwd: pathlib.Path, *, staged: bool) -> None:
        self.cwd = cwd
        self.staged = staged
        self.entries: dict[str, tuple[bytes, bytes]] = {}
        output = self.git("ls-files", "--stage", "--full-name", "-z")
        for record in output.split(b"\0"):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, oid, stage = metadata.split()
            path = name.decode("utf-8", "surrogateescape")
            self.entries[path] = (mode, oid if stage == b"0" else b"")

    def git(self, *args: str) -> bytes:
        return subprocess.run(["git", *args], cwd=self.cwd, check=True, capture_output=True).stdout

    def worktree_path(self, path: str) -> pathlib.Path:
        candidate = self.cwd
        for part in pathlib.PurePosixPath(path).parts:
            candidate /= part
            if candidate.is_symlink() or candidate.is_junction():
                raise VerificationSyncError(f"リンクは検証対象に指定できません: {path}")
        return candidate

    def read(self, path: str) -> bytes:
        mode, oid = self.entries[path]
        if mode not in {b"100644", b"100755"} or not oid:
            raise VerificationSyncError(f"通常の追跡ファイルではありません: {path}")
        if self.staged:
            return self.git("cat-file", "blob", oid.decode("ascii"))
        candidate = self.worktree_path(path)
        if not candidate.is_file():
            raise VerificationSyncError(f"通常の作業ツリーファイルではありません: {path}")
        return candidate.read_bytes()


def find_root(cwd: pathlib.Path) -> pathlib.Path | None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
        capture_output=True,
    )
    return pathlib.Path(result.stdout.decode("utf-8", "surrogateescape").rstrip("\r\n")) if result.returncode == 0 else None


def _frontmatter(content: bytes) -> tuple[dict[str, object], bytes]:
    normalized = content.replace(b"\r\n", b"\n")
    if not normalized.startswith(b"---\n"):
        return {}, normalized
    header, delimiter, body = normalized[4:].partition(b"\n---\n")
    if not delimiter:
        raise VerificationSyncError("frontmatter が閉じられていません")
    values = yaml.safe_load(header)
    if values is None:
        return {}, body
    if not isinstance(values, dict):
        raise VerificationSyncError("frontmatter はマッピングで記述してください")
    return values, body


def _matches(path: str, pattern: str) -> bool:
    parts = path.split("/")
    patterns = pattern.split("/")
    if "**" not in patterns:
        return len(parts) == len(patterns) and all(fnmatch.fnmatchcase(p, g) for p, g in zip(parts, patterns, strict=True))
    for index, glob in enumerate(patterns):
        if glob == "**":
            if len(parts) < index:
                return False
            if not all(fnmatch.fnmatchcase(p, g) for p, g in zip(parts[:index], patterns[:index], strict=True)):
                return False
            tail = "/".join(patterns[index + 1 :])
            return not tail or any(_matches("/".join(parts[start:]), tail) for start in range(index, len(parts)))
    return False


def _digest(repo: RepositoryFiles, patterns: object) -> tuple[str, list[str]]:
    if (
        not isinstance(patterns, list)
        or not patterns
        or not all(
            isinstance(p, str) and p and not set(p.split("/")) & {"", ".", ".."} and "\\" not in p and ":" not in p
            for p in patterns
        )
    ):
        raise VerificationSyncError("verifies はリポジトリ相対パスまたはglobの空でない配列で指定してください")
    paths = sorted(path for path in repo.entries if any(_matches(path, p) for p in patterns))
    if not paths:
        raise VerificationSyncError("verifies に一致する追跡ファイルがありません")
    digest = hashlib.sha256()
    for path in paths:
        content = repo.read(path).replace(b"\r\n", b"\n")
        name = path.encode("utf-8", "surrogateescape")
        for value in (name, content):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    return digest.hexdigest(), paths


def check(cwd: pathlib.Path, *, staged: bool) -> list[str]:
    root = find_root(cwd)
    if root is None:
        return []
    repo = RepositoryFiles(root, staged=staged)
    diagnostics = []
    for path in sorted(repo.entries):
        if not _matches(path, ".claude/skills/*/SKILL.md"):
            continue
        if repo.entries[path][0] not in {b"100644", b"100755"}:
            continue
        try:
            content = repo.read(path)
            try:
                metadata, _body = _frontmatter(content)
            except (VerificationSyncError, yaml.YAMLError):
                if b"verifies" not in content and b"verified-digest" not in content:
                    continue
                raise
            keys = {"verifies", "verified-digest"}
            missing = keys - metadata.keys()
            if missing == keys:
                continue
            if missing:
                raise VerificationSyncError(f"不足しているキー: {', '.join(sorted(missing))}")
            digest, paths = _digest(repo, metadata["verifies"])
            if metadata["verified-digest"] != digest:
                raise VerificationSyncError(
                    f"記録値: {metadata['verified-digest']!r}、現在値: {digest}、一致したファイル: {', '.join(paths)}。"
                    "スキルに従って検証を再実行・追従し、確かめた後に "
                    f"pyfltr verification-sync --update {path} を実行してください。"
                )
        except (VerificationSyncError, OSError, subprocess.CalledProcessError, yaml.YAMLError) as error:
            diagnostics.append(f"{path}: {error}")
    return diagnostics


def update(skill: pathlib.Path) -> None:
    root = find_root(pathlib.Path.cwd())
    if root is None:
        raise VerificationSyncError("Gitの作業ツリーで実行してください")
    path = skill.absolute().relative_to(root).as_posix()
    repo = RepositoryFiles(root, staged=False)
    if not _matches(path, ".claude/skills/*/SKILL.md") or path not in repo.entries:
        raise VerificationSyncError("追跡対象の .claude/skills/*/SKILL.md を指定してください")
    content = repo.read(path)
    metadata, _body = _frontmatter(content)
    digest, _paths = _digest(repo, metadata.get("verifies"))
    header = content.replace(b"\r\n", b"\n").split(b"\n---\n", 1)[0]
    parsed = yaml.compose(header[4:])
    if not isinstance(parsed, yaml.MappingNode) or parsed.flow_style:
        raise VerificationSyncError("frontmatter はマッピングで記述してください")
    lines = content.splitlines(keepends=True)
    newline = b"\r\n" if lines[0].endswith(b"\r\n") else b"\n"
    replacement = b"verified-digest: " + digest.encode("ascii") + newline
    digest_nodes = [
        (key, value) for key, value in parsed.value if isinstance(key, yaml.ScalarNode) and key.value == "verified-digest"
    ]
    if digest_nodes:
        key, value = digest_nodes[0]
        start = key.start_mark.line + 1
        if (
            len(digest_nodes) != 1
            or not isinstance(value, yaml.ScalarNode)
            or key.start_mark.line != value.start_mark.line
            or value.start_mark.line != value.end_mark.line
            or value.start_mark.index <= key.end_mark.index
            or not lines[start].startswith(b"verified-digest:")
        ):
            raise VerificationSyncError("verified-digest は単一の行で記述してください")
        lines[start] = replacement
    else:
        lines.insert(1, replacement)
    updated = b"".join(lines)
    updated_metadata, updated_body = _frontmatter(updated)
    if updated_metadata != metadata | {"verified-digest": digest} or updated_body != _body:
        raise VerificationSyncError("verified-digest 以外の内容が変化するため更新できません")
    repo.worktree_path(path).write_bytes(updated)
