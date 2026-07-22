"""サブプロジェクト検出・分類・uv workspace解釈・uv.lock親探索。

モノレポ対応の中核モジュール。起点 cwd 配下を再帰探索してマーカー
（固定ファイル名`pyproject.toml`・`Cargo.toml`、globパターン`*.csproj`・`*.sln`）を
持つディレクトリをサブプロジェクトとして列挙し、uv workspace の `members` glob を
含めて解決する。ネストする子サブプロジェクト配下のファイルは
`classify_files_by_subproject` の最深一致で子へ分類し、親の集合には含めない。

設計上の判断:

- マーカー（固定ファイル名`pyproject.toml`・`Cargo.toml`、globパターン`*.csproj`・
  `*.sln`）の存在で検出する（`[tool.pyfltr]`の有無は問わない）。`.NET`のプロジェクト/
  ソリューションファイルは利用者ごとにファイル名が異なるためglobパターンで判定する。
  `package.json`はプロジェクト境界を持たない汎用ファイルとして誤検出リスクが高いため
  対象外とする
- 検出結果が0件または1件の場合はモノレポモード非適用（呼び出し側で扱う）
- 除外パターンは `subproject-exclude` 設定で拡張可能（既定で `.venv`・`node_modules`・
  `target`・`build`・`dist`・`.git` を除外）
- `.gitignore` を尊重しつつ（`subproject-use-gitignore`）、規定ブラックリストも除外する
- uv workspace の `[tool.uv.workspace]` は `pyproject.toml` の解釈に限定し、
  他のマーカーのみを持つディレクトリには適用しない
  （`_read_uv_workspace_members`が`pyproject.toml`を直接読むため）
- `find_uv_lock_for_cwd` は cwd 直下に `uv.lock` がなければ workspace root まで親方向探索する
"""

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

if typing.TYPE_CHECKING:
    import pyfltr.config.config

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Subproject:
    """サブプロジェクト1件分の情報。

    `cwd` は絶対パス、`relative` は起点 cwd からの相対パス（POSIX 区切り）。
    `uv_workspace_root` は uv workspace member のときに workspace root の絶対パスを保持し、
    `find_uv_lock_for_cwd` の親方向探索の上限として使う。member でなければ `None`。
    """

    cwd: pathlib.Path
    """サブプロジェクトの絶対パス。"""
    relative: str
    """起点 cwd からの POSIX 区切り相対パス。`"."`（起点 cwd 自身）を含む。"""
    uv_workspace_root: pathlib.Path | None = None
    """uv workspace member の場合、workspace root の絶対パス。member でなければ `None`。"""


_DEFAULT_EXCLUDE_NAMES: tuple[str, ...] = (".venv", "node_modules", "target", "build", "dist", ".git")
"""サブプロジェクト探索時に再帰侵入しないディレクトリ名の規定リスト。

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
    """ディレクトリがサブプロジェクト検出マーカーを持つか判定する。

    固定ファイル名（`_SUBPROJECT_MARKER_NAMES`）またはglobパターン
    （`_SUBPROJECT_MARKER_GLOBS`）のいずれか1つでも一致すればサブプロジェクトとして扱う。
    `path` はディレクトリの絶対パスを想定する。パーミッションエラー時は False を返す。
    本関数自体は `path` やマーカーファイルのシンボリックリンクを判定・除外しない
    （`is_file()` / `glob()` の標準挙動どおりリンク先を実体として判定する）。
    ディレクトリのシンボリックリンクへの再帰侵入は `_walk_subproject_candidates` 側で除外する。
    """
    try:
        if any((path / name).is_file() for name in _SUBPROJECT_MARKER_NAMES):
            return True
        return any(any(p.is_file() for p in path.glob(pattern)) for pattern in _SUBPROJECT_MARKER_GLOBS)
    except OSError:
        return False


def _read_cargo_workspace_members(path: pathlib.Path) -> set[pathlib.Path] | None:
    """`path/Cargo.toml`が`[workspace]`を持つ場合、登録member crateの絶対パス集合を返す。

    `[workspace] members`のglob展開結果から`exclude`のglob展開結果を差し引く。
    workspace未定義・パース失敗時はNoneを返す（呼び出し側で「workspace rootではない」と判定）。
    """
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
    r"""`path`直下の全`.sln`ファイル内で登録されているproject所在ディレクトリの絶対パス集合を返す。

    solution不在・読込失敗時は空集合を返す。パス区切りは`\\`・`/`両対応（.sln慣習に従う）。
    """
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
            # 抽出対象は`*.csproj`等のファイルパス。所在ディレクトリを除外対象とする
            result.add(project_path.parent)
    return result


def _filter_aggregate_children(candidates: set[pathlib.Path]) -> set[pathlib.Path]:
    """集約ルート配下の登録済み下位マーカー（member crate・solution登録project）を除外する。

    - Cargo workspace root配下の登録member crateディレクトリ（`pyproject.toml`を持たない）は除外
    - `.sln`所在ディレクトリ配下の登録csproj所在ディレクトリ（`pyproject.toml`・`Cargo.toml`を
      持たない）は除外
    無登録の`Cargo.toml`・`*.csproj`（member登録なし・solution未登録）は独立サブプロジェクトとして残す。
    `pyproject.toml`を持つディレクトリは集約ルート配下でも独立サブプロジェクトとして残す。
    """
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
    config: pyfltr.config.config.Config,
    *,
    git_check_ignore: typing.Callable[[pathlib.Path, list[pathlib.Path]], set[pathlib.Path]] | None = None,
) -> list[Subproject]:
    """起点 cwd 配下を再帰探索し、サブプロジェクト一覧を返す。

    検出フロー:
    1. `start_cwd` 自身がマーカー（固定ファイル名またはglobパターン）を持てば候補に加える
    2. uv workspace の `[tool.uv.workspace] members` glob を展開して候補に加える
       （`pyproject.toml`側のみ対象）
    3. 残りのサブディレクトリを再帰探索し、マーカーを持つディレクトリを候補に加える
    4. 除外パターン（規定ブラックリスト + `subproject-exclude`）と `.gitignore` を尊重する
    5. 集約ルート（Cargo workspace root・`.sln`所在ディレクトリ）配下の下位マーカー
       （`Cargo.toml`単独・`*.csproj`単独）は候補から除外する（重複実行防止）

    ネストする子サブプロジェクト配下のファイル帰属は走査側では分離せず、`classify_files_by_subproject`
    の最深一致で子サブプロジェクトへ割り当てる（親側の集合からは結果的に除かれる）。

    `git_check_ignore` はテスト用の差し替えフック。実運用では `None` を渡し、内部で
    `subprocess.run(["git", "check-ignore", ...], cwd=start_cwd)` を呼ぶ。

    返り値は `Subproject` のリストで、`relative` パス文字列の自然順でソートする。
    """
    extra_excludes: list[str] = list(config.values.get("subproject-exclude", []))
    use_gitignore: bool = bool(config.values.get("subproject-use-gitignore", True))
    use_uv_workspace: bool = bool(config.values.get("subproject-uv-workspace", True))
    exclude_names: set[str] = set(_DEFAULT_EXCLUDE_NAMES) | set(extra_excludes)

    candidates: set[pathlib.Path] = set()

    if is_subproject_dir(start_cwd):
        candidates.add(start_cwd.resolve())

    # uv workspace の members glob 展開
    workspace_members: dict[pathlib.Path, pathlib.Path] = {}
    if use_uv_workspace and is_subproject_dir(start_cwd):
        workspace_root_real = start_cwd.resolve()
        member_paths = _read_uv_workspace_members(start_cwd)
        for member in member_paths:
            if is_subproject_dir(member):
                resolved = member.resolve()
                candidates.add(resolved)
                workspace_members[resolved] = workspace_root_real

    # 再帰探索
    for path in _walk_subproject_candidates(start_cwd, exclude_names):
        candidates.add(path.resolve())

    # gitignore 除外
    if use_gitignore:
        ignored = _filter_ignored_subprojects(start_cwd, list(candidates), git_check_ignore)
        candidates = candidates - ignored

    # 集約ルート（Cargo workspace root・.sln所在ディレクトリ）配下の登録済み下位マーカーを除外する
    candidates = _filter_aggregate_children(candidates)

    # 起点 cwd を含む全候補から、ネストの親（より上位）が候補に含まれる場合に
    # 内側を残しつつ親側の走査ロジックでは内側を除外する設計のため、ここでは
    # 全候補をそのままサブプロジェクト集合とする。子サブプロジェクト配下の
    # ファイル走査除外は `classify_files_by_subproject` 側で最深一致により実現する。
    subprojects: list[Subproject] = []
    start_real = start_cwd.resolve()
    for cwd in candidates:
        try:
            rel = cwd.relative_to(start_real)
        except ValueError:
            continue
        # `pathlib.Path.relative_to` は同一パス比較で `"."` を返すため、空文字列分岐は不要。
        rel_str = str(rel).replace("\\", "/")
        workspace_root = workspace_members.get(cwd)
        subprojects.append(Subproject(cwd=cwd, relative=rel_str, uv_workspace_root=workspace_root))

    subprojects = natsort.natsorted(subprojects, key=lambda s: s.relative)
    return subprojects


def _walk_subproject_candidates(
    start: pathlib.Path,
    exclude_names: set[str],
) -> typing.Iterator[pathlib.Path]:
    """再帰探索でマーカー（固定ファイル名またはglobパターン）を持つディレクトリを列挙する。

    探索中にマーカーを見つけた場合、当該ディレクトリは候補に加えるが
    配下への再帰侵入は継続する（ネストするサブプロジェクトを全て列挙するため）。
    `exclude_names` に名前一致するディレクトリは侵入しない。シンボリックリンクは辿らない。
    """
    stack: list[pathlib.Path] = [start]
    while stack:
        current = stack.pop()
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
            if is_subproject_dir(entry):
                yield entry
            stack.append(entry)


def _read_uv_workspace_members(root: pathlib.Path) -> list[pathlib.Path]:
    """`pyproject.toml` の `[tool.uv.workspace] members` を glob 展開して返す。

    `members` キー未定義時は空リストを返す。`exclude` キーは別キーで指定された
    glob を members 展開後に除去する。
    """
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
    # exclude glob を適用
    for pattern in excludes:
        for match in root.glob(pattern):
            collected.discard(match)
    return sorted(collected)


def _filter_ignored_subprojects(
    start_cwd: pathlib.Path,
    candidates: list[pathlib.Path],
    git_check_ignore: typing.Callable[[pathlib.Path, list[pathlib.Path]], set[pathlib.Path]] | None,
) -> set[pathlib.Path]:
    """候補のうち `.gitignore` で除外されているものを返す。

    `git_check_ignore` が指定されていれば実体に委ねる。未指定時は `subprocess.run` で
    `git check-ignore --stdin -z` を `cwd=start_cwd` で起動する。
    git 未導入・タイムアウト・想定外returncode時は空集合を返す（除外スキップ）。
    """
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
    """ファイル一覧をサブプロジェクト別の辞書と外部パス一覧へ分類して返す。

    各ファイルは「実体パスがどのサブプロジェクト cwd 配下に最も深く含まれるか」で
    最深一致のサブプロジェクトに割り当てる。これによりネストする子サブプロジェクト
    配下のファイルは親サブプロジェクトの集合から除外され、子サブプロジェクトの
    集合へ含まれる。

    `files` は起点 cwd 相対の `pathlib.Path` を想定する。返り値の辞書値は同じ起点 cwd 相対の
    ファイルパス一覧。サブプロジェクトに属さない（起点 cwd の外側等）ファイルは
    辞書からは外し、第2戻り値の外部パス一覧へ保持する。
    外部パス一覧は注入対象ツール（`config_arg_template`指定のmarkdownlint・textlint等）
    の追加実行や、除外対象ツール（`allows_external_paths=False`）の警告発行に使う。

    `subprojects` が空の場合は空辞書と空リストを返す（呼び出し側は単一実行経路へフォールバック）。
    """
    if not subprojects:
        return {}, []
    # cwd の depth（パス要素数）が深いほど優先する。
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
        else:
            external.append(f)
    return result, external


def find_uv_lock_for_cwd(cwd: pathlib.Path, *, workspace_root: pathlib.Path | None = None) -> pathlib.Path | None:
    """`cwd` 配下に `uv.lock` があれば返す。無ければ workspace root まで親方向探索する。

    `workspace_root` が指定されていない場合は cwd 直下のみ確認する。
    親方向探索は workspace root を越えない（祖先ディレクトリへの越境を防ぐ）。
    `Subproject.uv_workspace_root` から取得した値を渡す想定。
    """
    direct = cwd / "uv.lock"
    if direct.is_file():
        return direct
    if workspace_root is None:
        return None
    # 親方向探索（workspace_root を含む）
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
