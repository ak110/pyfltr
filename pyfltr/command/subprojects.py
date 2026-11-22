from __future__ import annotations

import dataclasses
import logging
import pathlib
import re
import subprocess
import typing

import natsort
import tomlkit
import tomlkit.exceptions

import pyfltr.command.targets
import pyfltr.config.model
import pyfltr.paths

if typing.TYPE_CHECKING:
    import pyfltr.config.config

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Subproject:
    cwd: pathlib.Path
    """サブプロジェクトの絶対パス。"""
    relative: str
    """起点 cwd からの POSIX 区切り相対パス。`"."`（起点 cwd 自身）を含む。"""
    uv_workspace_root: pathlib.Path | None = None
    """uv workspace member の場合、workspace root の絶対パス。member でなければ `None`。"""


_DEFAULT_EXCLUDE_NAMES: tuple[str, ...] = (".venv", "node_modules", "target", "build", "dist", ".git")
"""サブプロジェクト探索時に再帰侵入しないディレクトリ名の規定リスト。

ファイル展開の除外（`exclude`・`extend-exclude`）に加えて常に適用する。
`subproject-exclude` 設定キーで追加除外できる。
"""


_SUBPROJECT_MARKER_NAMES: tuple[str, ...] = ("pyproject.toml", "Cargo.toml")
"""固定ファイル名で判定するサブプロジェクト検出マーカー。

言語別に複数許容する設計とする。新言語対応時は本タプルへの追加のみで拡張できる。
"""

_SUBPROJECT_MARKER_GLOBS: tuple[str, ...] = ("*.csproj", "*.sln")
"""globパターンで判定するサブプロジェクト検出マーカー。

`.NET`のプロジェクト/ソリューションファイルは利用者ごとにファイル名が異なるため、
固定ファイル名では判定できずglobパターンで判定する。`package.json`は
プロジェクト境界を持たない汎用ファイルとして誤検出リスクが高いため対象外とする
（判断根拠は`### エージェント判断`・`### 却下した代替案`を参照）。
"""


def is_subproject_dir(path: pathlib.Path) -> bool:
    try:
        if any((path / name).is_file() for name in _SUBPROJECT_MARKER_NAMES):
            return True
        return any(any(p.is_file() for p in path.glob(pattern)) for pattern in _SUBPROJECT_MARKER_GLOBS)
    except OSError:
        return False


def _read_cargo_workspace_members(path: pathlib.Path) -> set[pathlib.Path] | None:
    cargo_toml = path / "Cargo.toml"
    try:
        text = cargo_toml.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)
    except (OSError, tomlkit.exceptions.TOMLKitError):
        return None
    workspace = doc.get("workspace")
    if not isinstance(workspace, dict):
        return None
    members_raw = workspace.get("members", [])
    exclude_raw = workspace.get("exclude", [])
    members: list[str] = [str(m) for m in members_raw] if isinstance(members_raw, list) else []
    excludes: list[str] = [str(m) for m in exclude_raw] if isinstance(exclude_raw, list) else []
    collected: set[pathlib.Path] = set()
    for pattern in members:
        for match in path.glob(pattern):
            if match.is_dir():
                collected.add(match.resolve())
    for pattern in excludes:
        for match in path.glob(pattern):
            collected.discard(match.resolve())
    return collected


_DOTNET_SLN_PROJECT_LINE = re.compile(
    r'^Project\("\{[0-9A-Fa-f-]+\}"\)\s*=\s*"[^"]*"\s*,\s*"([^"]+)"\s*,\s*"\{[0-9A-Fa-f-]+\}"',
    re.MULTILINE,
)
"""`.sln`の`Project(...) = ..., "relative/path/to/AppA.csproj", ...`行を抽出する正規表現。"""


def _read_dotnet_solution_projects(path: pathlib.Path) -> set[pathlib.Path]:
    result: set[pathlib.Path] = set()
    try:
        sln_files = list(path.glob("*.sln"))
    except OSError:
        return result
    for sln in sln_files:
        try:
            text = sln.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for match in _DOTNET_SLN_PROJECT_LINE.finditer(text):
            rel = match.group(1).replace("\\", "/")
            project_path = (path / rel).resolve()
            result.add(project_path.parent)
    return result


def _filter_aggregate_children(candidates: set[pathlib.Path]) -> set[pathlib.Path]:
    cargo_excluded: set[pathlib.Path] = set()
    dotnet_excluded: set[pathlib.Path] = set()
    for cand in candidates:
        members = _read_cargo_workspace_members(cand)
        if members is not None:
            cargo_excluded |= members
        dotnet_excluded |= _read_dotnet_solution_projects(cand)
    kept: set[pathlib.Path] = set()
    for cand in candidates:
        cand_real = cand.resolve()
        has_pyproject = (cand / "pyproject.toml").is_file()
        if has_pyproject:
            kept.add(cand)
            continue
        has_cargo = (cand / "Cargo.toml").is_file()
        has_csproj = any(cand.glob("*.csproj"))
        if has_cargo and cand_real in cargo_excluded:
            continue
        if has_csproj and not has_cargo and cand_real in dotnet_excluded:
            continue
        kept.add(cand)
    return kept


def discover_subprojects(
    start_cwd: pathlib.Path,
    config: pyfltr.config.model.Config,
    *,
    git_check_ignore: typing.Callable[[pathlib.Path, list[pathlib.Path]], set[pathlib.Path]] | None = None,
) -> list[Subproject]:
    extra_excludes: list[str] = list(config.values.get("subproject-exclude", []))
    use_gitignore: bool = bool(config.values.get("subproject-use-gitignore", True))
    use_uv_workspace: bool = bool(config.values.get("subproject-uv-workspace", True))
    exclude_names: set[str] = set(_DEFAULT_EXCLUDE_NAMES) | set(extra_excludes)

    candidates: set[pathlib.Path] = set()

    if is_subproject_dir(start_cwd):
        candidates.add(start_cwd.resolve())

    workspace_members: dict[pathlib.Path, pathlib.Path] = {}
    if use_uv_workspace and is_subproject_dir(start_cwd):
        workspace_root_real = start_cwd.resolve()
        member_paths = _read_uv_workspace_members(start_cwd)
        for member in member_paths:
            if is_subproject_dir(member):
                resolved = member.resolve()
                candidates.add(resolved)
                workspace_members[resolved] = workspace_root_real

    for path in _walk_subproject_candidates(start_cwd, exclude_names, config):
        candidates.add(path.resolve())

    if use_gitignore:
        ignored = _filter_ignored_subprojects(start_cwd, list(candidates), git_check_ignore)
        candidates = candidates - ignored

    candidates = _filter_aggregate_children(candidates)

    subprojects: list[Subproject] = []
    start_real = start_cwd.resolve()
    for cwd in candidates:
        try:
            rel = cwd.relative_to(start_real)
        except ValueError:
            continue
        rel_str = str(rel).replace("\\", "/")
        workspace_root = workspace_members.get(cwd)
        subprojects.append(Subproject(cwd=cwd, relative=rel_str, uv_workspace_root=workspace_root))

    subprojects = natsort.natsorted(subprojects, key=lambda s: s.relative)
    return subprojects


def _walk_subproject_candidates(
    start: pathlib.Path,
    exclude_names: set[str],
    config: pyfltr.config.model.Config,
) -> typing.Iterator[pathlib.Path]:
    stack: list[tuple[pathlib.Path, pathlib.Path]] = [(start, pathlib.Path())]
    while stack:
        current, current_rel = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.name in exclude_names:
                continue
            try:
                if entry.is_symlink():
                    continue
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            entry_rel = current_rel / entry.name
            if pyfltr.command.targets.excluded(entry_rel, config) is not None:
                continue
            if is_subproject_dir(entry):
                yield entry
            stack.append((entry, entry_rel))


def _read_uv_workspace_members(root: pathlib.Path) -> list[pathlib.Path]:
    pyproject_path = root / "pyproject.toml"
    try:
        text = pyproject_path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)
    except (OSError, tomlkit.exceptions.TOMLKitError):
        return []
    tool = doc.get("tool", {})
    if not isinstance(tool, dict):
        return []
    uv = tool.get("uv", {})
    if not isinstance(uv, dict):
        return []
    workspace = uv.get("workspace", {})
    if not isinstance(workspace, dict):
        return []
    members_raw = workspace.get("members", [])
    exclude_raw = workspace.get("exclude", [])
    members: list[str] = [str(m) for m in members_raw] if isinstance(members_raw, list) else []
    excludes: list[str] = [str(m) for m in exclude_raw] if isinstance(exclude_raw, list) else []

    collected: set[pathlib.Path] = set()
    for pattern in members:
        for match in root.glob(pattern):
            if match.is_dir():
                collected.add(match)
    for pattern in excludes:
        for match in root.glob(pattern):
            collected.discard(match)
    return sorted(collected)


def _filter_ignored_subprojects(
    start_cwd: pathlib.Path,
    candidates: list[pathlib.Path],
    git_check_ignore: typing.Callable[[pathlib.Path, list[pathlib.Path]], set[pathlib.Path]] | None,
) -> set[pathlib.Path]:
    if not candidates:
        return set()
    if git_check_ignore is not None:
        return git_check_ignore(start_cwd, candidates)
    start_real = start_cwd.resolve()
    rel_inputs: list[tuple[pathlib.Path, str]] = []
    for cand in candidates:
        try:
            rel = cand.relative_to(start_real)
        except ValueError:
            continue
        rel_str = str(rel).replace("\\", "/")
        if rel_str in ("", "."):
            continue
        rel_inputs.append((cand, rel_str))
    if not rel_inputs:
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(s for _, s in rel_inputs),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            cwd=start_cwd,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()
    if proc.returncode not in (0, 1):
        return set()
    ignored_strs = {s for s in proc.stdout.split("\0") if s}
    return {cand for cand, rel_str in rel_inputs if rel_str in ignored_strs}


def classify_files_by_subproject(
    files: list[pathlib.Path],
    subprojects: list[Subproject],
    start_cwd: pathlib.Path,
) -> tuple[dict[pathlib.Path, list[pathlib.Path]], list[pathlib.Path]]:
    if not subprojects:
        return {}, []
    sorted_subs = sorted(subprojects, key=lambda s: len(s.cwd.parts), reverse=True)
    start_real = start_cwd.resolve()
    result: dict[pathlib.Path, list[pathlib.Path]] = {s.cwd: [] for s in subprojects}
    external: list[pathlib.Path] = []
    for f in files:
        try:
            abs_path = (start_real / f).resolve() if not f.is_absolute() else f.resolve()
        except OSError:
            abs_path = (start_real / f) if not f.is_absolute() else f
        chosen: pathlib.Path | None = None
        for sub in sorted_subs:
            try:
                abs_path.relative_to(sub.cwd)
            except ValueError:
                continue
            chosen = sub.cwd
            break
        if chosen is not None:
            result[chosen].append(f)
        elif pyfltr.paths.is_external_path(f, start_cwd=start_real):
            external.append(f)
        else:
            result.setdefault(start_real, []).append(f)
    return result, external


def find_uv_lock_for_cwd(cwd: pathlib.Path, *, workspace_root: pathlib.Path | None = None) -> pathlib.Path | None:
    direct = cwd / "uv.lock"
    if direct.is_file():
        return direct
    if workspace_root is None:
        return None
    cwd_real = cwd.resolve()
    root_real = workspace_root.resolve()
    if cwd_real == root_real:
        return None
    try:
        cwd_real.relative_to(root_real)
    except ValueError:
        return None
    current = cwd_real.parent
    while True:
        candidate = current / "uv.lock"
        if candidate.is_file():
            return candidate
        if current == root_real:
            return None
        if current.parent == current:
            return None
        try:
            current.relative_to(root_real)
        except ValueError:
            return None
        current = current.parent
