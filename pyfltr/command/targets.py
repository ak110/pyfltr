import contextlib
import functools
import os
import pathlib
import subprocess
import typing

import pyfltr.config.config
import pyfltr.config.model
import pyfltr.paths
import pyfltr.warnings_

if typing.TYPE_CHECKING:
    import pyfltr.command.only_failed

logger = __import__("logging").getLogger(__name__)


def pick_targets(
    only_failed_targets: "dict[str, pyfltr.command.only_failed.ToolTargets] | None",
    command: str,
) -> "pyfltr.command.only_failed.ToolTargets | None":
    if only_failed_targets is None:
        return None
    return only_failed_targets.get(command)


def expand_all_files(
    targets: list[pathlib.Path],
    config: pyfltr.config.model.Config,
    *,
    start_cwd: pathlib.Path | None = None,
    exclude_subdirs: list[pathlib.Path] | None = None,
) -> list[pathlib.Path]:
    """対象ファイルの一括展開。

    ディレクトリ走査・excludeチェック・gitignoreフィルタリングを1回だけ実行し、
    全ファイルのリストを返す。コマンドごとのglobフィルタリングは `filter_by_globs` で行う。

    `start_cwd` は走査・パス正規化・git連携の起点 cwd（絶対パス）。`None` の場合は
    `pathlib.Path.cwd()` を使う（既存挙動）。サブプロジェクト分割実行を含むパイプライン
    では `run_pipeline` 開始時に確定した cwd を明示する。

    `exclude_subdirs` には走査時に侵入を避けたいサブディレクトリ（絶対パスの一覧）を渡す。
    モノレポでネストする子サブプロジェクト配下を親側の走査から除外する目的だが、最終的な
    サブプロジェクト分類は `classify_files_by_subproject` の最深一致で行うため、本引数は
    走査効率を上げる補助手段に留める。`None` の場合は除外なし。

    シンボリックリンクディレクトリは原則として配下を辿る。
    `respect-gitignore=True` 下では走査前に対象のディレクトリ自身の `is_symlink()` を判定し、
    末尾 `/` を付けない単一パス指定で `git check-ignore` へ問い合わせ、ignoredなら配下を辿らない。
    早期スキップが機能するのは `.gitignore` のファイル形式パターン（`name`・`*pattern*` 等）に
    限定される。`link/` 形式のディレクトリ専用パターンは `git check-ignore` がシンボリックリンク越え
    のpathspecを拒否するため判定不可となり、早期スキップは機能しない。

    位置引数の相対パスは起点内で畳み込める`..`を字句的に畳み込んだ表現へ置き換える（`_collapse_relative_dots`）。
    返り値は起点内の`..`を持たず、起点外を指す相対パス（`../outside/a.md`など）は利用者が渡した表現を保つ。
    別の出所のパスと突き合わせる処理は、両側を`pyfltr.paths.to_match_key`で揃えてから比べる。

    最終出力は `_dedup_and_sort` が実体パス単位で重複排除し、パス文字列で安定ソートして返す。
    リポジトリ内のシンボリックリンク再配置等で同一実体ファイルが複数パスから列挙される構成でも、
    ツールが同じ内容を多重チェックすることを避ける。

    cwdリポジトリ内/外の判定とサイレント素通し回避の挙動については `_filter_by_gitignore`
    のdocstringを参照する。
    """
    cwd_base = start_cwd if start_cwd is not None else pathlib.Path.cwd()
    excluded_real: set[pathlib.Path] = set()
    if exclude_subdirs:
        for sub in exclude_subdirs:
            try:
                excluded_real.add(sub.resolve())
            except OSError:
                excluded_real.add(sub)

    if len(targets) == 0:
        targets = [pathlib.Path(".")]

    directly_specified: set[pathlib.Path] = set()
    expanded: list[pathlib.Path] = []
    respect_gitignore = bool(config["respect-gitignore"])
    exclude_matchers: dict[tuple[str, pathlib.Path], typing.Callable[[pathlib.Path], str | None]] = {}

    def _excluded_for_run(path: pathlib.Path, base_dir: pathlib.Path) -> tuple[str, str] | None:
        for key in ("exclude", "extend-exclude"):
            cache_key = (key, base_dir)
            if cache_key not in exclude_matchers:
                exclude_matchers[cache_key] = _make_exclude_matcher(tuple(config[key]), base_dir=base_dir)
            matcher = exclude_matchers[cache_key]
            matched = matcher(path)
            if matched is not None:
                return (key, matched)
        return None

    def _is_in_excluded_subdir(path: pathlib.Path) -> bool:
        if not excluded_real:
            return False
        try:
            abs_path = (cwd_base / path).resolve() if not path.is_absolute() else path.resolve()
        except OSError:
            abs_path = (cwd_base / path) if not path.is_absolute() else path
        return any(_is_subpath(abs_path, sub) for sub in excluded_real)

    def _expand_target(target: pathlib.Path, *, is_direct: bool, base_dir: pathlib.Path) -> None:
        try:
            filesystem_target = target if target.is_absolute() else cwd_base / target
            if _is_in_excluded_subdir(target):
                return
            match = _excluded_for_run(target, base_dir)
            if match is not None:
                if is_direct:
                    key, pattern = match
                    normalized_target = pyfltr.paths.normalize_separators(target)
                    pyfltr.warnings_.emit_warning(
                        source="file-resolver",
                        message=(
                            f'指定されたファイルが除外設定により無視されました: {normalized_target} ({key}="{pattern}" による)'
                        ),
                        hint="除外設定を無視して検査する場合は `--no-exclude`（MCPでは`no_exclude=True`）を指定してください。",
                    )
                    pyfltr.warnings_.add_filtered_direct_file(normalized_target, reason="excluded")
                return
            if filesystem_target.is_dir():
                if respect_gitignore and filesystem_target.is_symlink() and _is_ignored_single_path(target, cwd=cwd_base):
                    return
                for child in filesystem_target.iterdir():
                    _expand_target(target / child.name, is_direct=False, base_dir=base_dir)
            else:
                expanded.append(target)
                if is_direct:
                    directly_specified.add(target)
        except OSError as e:
            normalized_target = pyfltr.paths.normalize_separators(target)
            logger.debug("対象ファイルの展開に失敗: %s", normalized_target, exc_info=True)
            pyfltr.warnings_.emit_warning(
                source="file-resolver",
                message=f"指定されたパスを読み取れないため対象から除外しました: {normalized_target}: {e.strerror or e}",
                hint="パスの存在と読み取り権限を確認してください。",
            )

    for target in targets:
        base_dir = exclude_base_dir(target, targets=targets, start_cwd=cwd_base)
        if target.is_absolute():
            with contextlib.suppress(ValueError):
                target = target.relative_to(cwd_base)
        target = _collapse_relative_dots(target, cwd_base=cwd_base)
        check_path = target if target.is_absolute() else (cwd_base / target)
        if not check_path.exists():
            normalized_target = pyfltr.paths.normalize_separators(target)
            pyfltr.warnings_.emit_warning(
                source="file-resolver",
                message=f"指定されたパスが見つかりません: {normalized_target}",
                hint="パスの綴りと、相対パスの基準になる起動したディレクトリを確認してください。",
            )
            pyfltr.warnings_.add_filtered_direct_file(normalized_target, reason="missing")
            continue
        is_direct = not check_path.is_dir()
        _expand_target(target, is_direct=is_direct, base_dir=base_dir)

    if respect_gitignore:
        before_gitignore = set(expanded)
        expanded = _filter_by_gitignore(expanded, cwd=cwd_base)
        after_set = set(expanded)
        for target in directly_specified:
            if target in before_gitignore and target not in after_set:
                normalized_target = pyfltr.paths.normalize_separators(target)
                pyfltr.warnings_.emit_warning(
                    source="file-resolver",
                    message=f"指定されたファイルが .gitignore により無視されました: {normalized_target}",
                    hint=(
                        ".gitignoreを無視して検査する場合は `--no-gitignore`（MCPでは`no_gitignore=True`）を指定してください。"
                    ),
                )
                pyfltr.warnings_.add_filtered_direct_file(normalized_target, reason="excluded")

    return _dedup_and_sort(expanded, cwd_base=cwd_base)


def _collapse_relative_dots(target: pathlib.Path, *, cwd_base: pathlib.Path) -> pathlib.Path:
    if target.is_absolute():
        return target
    collapsed = pathlib.Path(os.path.normpath(target))
    if collapsed == target:
        return target
    try:
        if (cwd_base / collapsed).resolve() != (cwd_base / target).resolve():
            return target
    except OSError:
        return target
    return collapsed


def _is_subpath(path: pathlib.Path, ancestor: pathlib.Path) -> bool:
    try:
        path.relative_to(ancestor)
    except ValueError:
        return False
    return True


def _dedup_and_sort(paths: list[pathlib.Path], *, cwd_base: pathlib.Path) -> list[pathlib.Path]:
    candidates: dict[pathlib.Path, list[pathlib.Path]] = {}
    for p in paths:
        filesystem_path = p if p.is_absolute() else cwd_base / p
        try:
            real = filesystem_path.resolve()
        except OSError:
            real = filesystem_path.absolute()
        candidates.setdefault(real, []).append(p)
    selection_key = functools.partial(_dedup_selection_key, cwd_base=cwd_base)
    deduped = [min(group, key=selection_key) for group in candidates.values()]
    deduped.sort(key=str)
    return deduped


def _dedup_selection_key(path: pathlib.Path, *, cwd_base: pathlib.Path) -> tuple[bool, str]:
    filesystem_path = path if path.is_absolute() else cwd_base / path
    try:
        is_link = filesystem_path.is_symlink()
    except OSError:
        is_link = True
    return (is_link, str(path))


def _is_ignored_single_path(path: pathlib.Path, *, cwd: pathlib.Path | None = None) -> bool:
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            capture_output=True,
            timeout=10,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _work_tree_state(cwd: pathlib.Path) -> typing.Literal["inside", "outside", "unknown"]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        return "unknown"
    if result.returncode == 0:
        return "inside" if result.stdout.strip() == "true" else "outside"
    git_dir = os.environ.get("GIT_DIR")
    if git_dir:
        git_dir_path = pathlib.Path(git_dir)
        if not git_dir_path.is_absolute():
            git_dir_path = cwd / git_dir_path
        probe_env = os.environ.copy()
        probe_env.pop("GIT_DIR", None)
        probe_env.pop("GIT_WORK_TREE", None)
        probe_env["GIT_CONFIG_NOSYSTEM"] = "1"
        probe_env["GIT_CONFIG_GLOBAL"] = os.devnull
        try:
            resolve_result = subprocess.run(
                ["git", "rev-parse", "--resolve-git-dir", str(git_dir_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
                cwd=str(cwd),
                env=probe_env,
            )
        except subprocess.TimeoutExpired:
            return "unknown"
        if resolve_result.returncode == 0:
            return "unknown"
    try:
        current = cwd.resolve()
    except OSError:
        current = cwd.absolute()
    if any((candidate / ".git").exists() for candidate in [current, *current.parents]):
        return "unknown"
    return "outside"


def _filter_by_gitignore(paths: list[pathlib.Path], *, cwd: pathlib.Path | None = None) -> list[pathlib.Path]:
    """Git check-ignoreで .gitignoreに該当するファイルを除外する。

    各入力パスを実体パスに正規化し、起点 cwd リポジトリからの相対パスへ変換した上で
    `git check-ignore --stdin -z`に渡す。シンボリックリンク越えのpathspecで
    `fatal: pathspec ... is beyond a symbolic link`が発生してreturncode 128となり
    .gitignore除外処理が丸ごとスキップされる事象を回避するため。
    起点 cwd リポジトリ外に解決されたパスは判定対象外として、入力パスをそのまま残す。

    `cwd` を指定すると `git` の作業ディレクトリと相対パス基準として使う。
    `None` の場合は親プロセスの cwd を使う（既存挙動）。

    サブプロセスのreturncodeが0でも1でもない場合は、returncodeが128かつ起点cwdがGit作業ツリー外と
    確定できた場合に限り、警告を発行せずフィルター適用を省略して入力パスをそのまま返す。
    作業ツリー内の128、内外を判定できない128、128以外の終了コードでは`emit_warning`でstderrを通知し、
    stdoutで返ってきた部分結果をignored判定として活用する。
    `logger.debug`のみで全パスを素通しさせるサイレントなフォールバックは
    .gitignore除外スキップを隠蔽する不具合の原因となるため採用しない。
    """
    if not paths:
        return paths
    cwd_base = cwd if cwd is not None else pathlib.Path.cwd()
    cwd_real = cwd_base.resolve()
    in_repo: list[tuple[pathlib.Path, str]] = []
    out_repo: list[pathlib.Path] = []
    for p in paths:
        try:
            abs_path = p if p.is_absolute() else (cwd_base / p)
            real = abs_path.resolve()
        except OSError:
            out_repo.append(p)
            continue
        try:
            rel = real.relative_to(cwd_real)
        except ValueError:
            out_repo.append(p)
            continue
        in_repo.append((p, str(rel)))

    if not in_repo:
        return paths

    rel_strs = [rel for _, rel in in_repo]
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(rel_strs),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            cwd=str(cwd_base),
        )
    except FileNotFoundError:
        pyfltr.warnings_.emit_warning(
            source="git",
            message="git が見つからないため respect-gitignore をスキップする。gitignore対象も検査に含めます",
            hint="gitを導入して再実行するか、設定の respect-gitignore = false で省略を明示してください。",
        )
        return paths
    except subprocess.TimeoutExpired:
        pyfltr.warnings_.emit_warning(
            source="git",
            message="git check-ignore がタイムアウトしたためスキップする。gitignore対象も検査に含めます",
            hint="`git check-ignore -v <パス>`で対象の判定が完了することを確認し、再実行してください。",
        )
        return paths
    if result.returncode not in (0, 1):
        if result.returncode == 128 and _work_tree_state(cwd_base) == "outside":
            return paths
        stderr_msg = result.stderr.strip() if result.stderr else ""
        detail = f": {stderr_msg}" if stderr_msg else ""
        pyfltr.warnings_.emit_warning(
            source="git",
            message=(
                f"git check-ignore が終了コード {result.returncode} を返したため "
                f"一部の .gitignore 判定がスキップされました{detail}。"
                "判定できなかったファイルは、.gitignoreの対象であっても検査対象に含めました"
            ),
            hint="`git status`でリポジトリの状態を確認し、`git check-ignore -v <パス>`で個別の判定を確認してください。",
        )
    ignored_set: set[str] = set()
    if result.stdout:
        ignored_set = {s for s in result.stdout.split("\0") if s}
    not_ignored_in_repo = [p for p, rel in in_repo if rel not in ignored_set]
    return not_ignored_in_repo + out_repo


def filter_by_globs(all_files: list[pathlib.Path], globs: list[str]) -> list[pathlib.Path]:
    return [f for f in all_files if any(f.match(glob) for glob in globs)]


def filter_by_changed_since(all_files: list[pathlib.Path], ref: str, *, cwd: pathlib.Path | None = None) -> list[pathlib.Path]:
    changed = _get_changed_files(ref, cwd=cwd)
    if changed is None:
        return all_files
    if not changed:
        return []
    start = (cwd if cwd is not None else pathlib.Path.cwd()).resolve()
    root = _get_git_toplevel(start)
    base = root if root is not None else start
    changed_keys = {pyfltr.paths.to_match_key(base / p, start_cwd=start) for p in changed}
    return [f for f in all_files if pyfltr.paths.to_match_key(f, start_cwd=start) in changed_keys]


def _get_git_toplevel(cwd: pathlib.Path) -> pathlib.Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            cwd=str(cwd),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    toplevel = result.stdout.strip()
    if result.returncode != 0 or not toplevel:
        return None
    return pathlib.Path(toplevel)


def _get_changed_files(ref: str, *, cwd: pathlib.Path | None = None) -> list[str] | None:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
            cwd=str(cwd) if cwd is not None else None,
        )
    except FileNotFoundError:
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"git が見つからないため --changed-since={ref!r} をスキップして全体実行します",
            hint="gitを導入するか、--changed-since（MCPではchanged_since）の指定を外して実行してください。",
        )
        return None
    except subprocess.TimeoutExpired:
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"git diff --name-only がタイムアウトしたため --changed-since={ref!r} をスキップして全体実行します",
            hint="`git diff --name-only <ref>`が完了することを確認して再実行してください。全体を検査する場合は対応不要です。",
        )
        return None
    if result.returncode != 0:
        stderr_msg = result.stderr.strip()
        detail = f": {stderr_msg}" if stderr_msg else ""
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"--changed-since={ref!r} の ref が解決できないためスキップして全体実行します{detail}",
            hint="`git rev-parse <ref>`で参照を確認し、--changed-since（MCPではchanged_since）を指定し直してください。",
        )
        return None
    return [p for p in result.stdout.split("\0") if p]


def emit_external_path_warning(command: str, normalized_target: str) -> None:
    pyfltr.warnings_.emit_warning(
        source="external-path",
        message=f"{command}: 起点cwd外のパスは対象から除外しました: {normalized_target}",
        hint=(
            "起点cwd外のパスを検査する場合は `--allow-external-paths`（MCPでは`allow_external_paths=True`）を指定し、"
            "`--work-dir`（MCPでは`work_dir`）へ検査設定を持つプロジェクトのルートを渡してください。"
        ),
    )
    pyfltr.warnings_.add_filtered_direct_file(normalized_target, reason="external")


def exclude_base_dir(path: pathlib.Path, *, targets: list[pathlib.Path], start_cwd: pathlib.Path) -> pathlib.Path:
    if not pyfltr.paths.is_external_path(path, start_cwd=start_cwd):
        return start_cwd
    bases = [target.parent for target in targets if target.is_absolute() and path.is_relative_to(target)]
    return max(bases, key=lambda base: len(base.parts), default=path.parent)


def matches_exclude_patterns(path: pathlib.Path, patterns: list[str], *, base_dir: pathlib.Path) -> str | None:
    return _make_exclude_matcher(tuple(patterns), base_dir=base_dir)(path)


def _make_exclude_matcher(patterns: tuple[str, ...], *, base_dir: pathlib.Path) -> typing.Callable[[pathlib.Path], str | None]:

    @functools.cache
    def _match(path: pathlib.Path) -> str | None:
        for glob in patterns:
            if path.match(glob):
                return glob
        parent = path.parent
        if parent == path or str(parent) == ".":
            return None
        return _match(parent)

    def _match_from_base(path: pathlib.Path) -> str | None:
        relative = pathlib.Path(os.path.relpath(path, base_dir)) if path.is_absolute() else path
        return _match(relative)

    return _match_from_base


def excluded(path: pathlib.Path, config: pyfltr.config.model.Config) -> tuple[str, str] | None:
    for key in ("exclude", "extend-exclude"):
        matched = matches_exclude_patterns(path, config[key], base_dir=pathlib.Path("."))
        if matched is not None:
            return (key, matched)
    return None
