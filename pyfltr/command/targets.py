"""対象ファイル選定。"""

import contextlib
import pathlib
import subprocess
import typing

import pyfltr.config.config
import pyfltr.paths
import pyfltr.warnings_

if typing.TYPE_CHECKING:
    import pyfltr.state.only_failed

logger = __import__("logging").getLogger(__name__)


def pick_targets(
    only_failed_targets: "dict[str, pyfltr.state.only_failed.ToolTargets] | None",
    command: str,
) -> "pyfltr.state.only_failed.ToolTargets | None":
    """`only_failed_targets` から当該ツールのToolTargetsを取り出す。

    `only_failed_targets` 自体が `None` の場合（`--only-failed` 未指定）は常に
    `None` を返し、`execute_command` で既定の `all_files` に委ねる。指定あり時は
    dictから当該コマンドのエントリを返す（存在しない場合はNone）。
    `cli` と `ui` の両経路から同一挙動で引ける共通ヘルパー。
    """
    if only_failed_targets is None:
        return None
    return only_failed_targets.get(command)


def expand_all_files(targets: list[pathlib.Path], config: pyfltr.config.config.Config) -> list[pathlib.Path]:
    """対象ファイルの一括展開。

    ディレクトリ走査・excludeチェック・gitignoreフィルタリングを1回だけ実行し、
    全ファイルのリストを返す。コマンドごとのglobフィルタリングは `filter_by_globs` で行う。

    シンボリックリンクディレクトリは原則として配下を辿る。
    `respect-gitignore=True` 下では走査前に当該ディレクトリ自身の `is_symlink()` を判定し、
    末尾 `/` を付けない単一パス指定で `git check-ignore` へ問い合わせ、ignoredなら配下を辿らない。
    早期スキップが機能するのは `.gitignore` のファイル形式パターン（`name`・`*pattern*` 等）に
    限定される。`link/` 形式のディレクトリ専用パターンは `git check-ignore` がシンボリックリンク越え
    のpathspecを拒否するため判定不可となり、早期スキップは機能しない。

    最終出力は `_dedup_and_sort` が実体パス単位で重複排除し、パス文字列で安定ソートして返す。
    リポジトリ内のシンボリックリンク再配置等で同一実体ファイルが複数パスから列挙される構成でも、
    ツールが同じ内容を多重チェックすることを避ける。

    cwdリポジトリ内/外の判定とサイレント素通し回避の挙動については `_filter_by_gitignore`
    のdocstringを参照する。
    """
    # 空ならカレントディレクトリを対象とする
    if len(targets) == 0:
        targets = [pathlib.Path(".")]

    # コマンドラインで直接指定されたファイル（ディレクトリでないもの）を記録
    directly_specified: set[pathlib.Path] = set()
    expanded: list[pathlib.Path] = []
    respect_gitignore = bool(config["respect-gitignore"])

    def _expand_target(target: pathlib.Path, *, is_direct: bool) -> None:
        try:
            match = excluded(target, config)
            if match is not None:
                if is_direct:
                    key, pattern = match
                    pyfltr.warnings_.emit_warning(
                        source="file-resolver",
                        message=(f'指定されたファイルが除外設定により無視されました: {target} ({key}="{pattern}" による)'),
                    )
                    pyfltr.warnings_.add_filtered_direct_file(str(target), reason="excluded")
                return
            if target.is_dir():
                # シンボリックリンクディレクトリ自身が.gitignore対象なら配下を辿らない。
                # 末尾`/`を付けない単一パス指定で問い合わせるため、
                # ファイル形式パターン（`name`・`*pattern*`等）に限り判定が成立する。
                # `link/`形式のディレクトリ専用パターンは`git check-ignore`が
                # symlink越えのpathspecを拒否するため判定不可となり、早期スキップは機能しない。
                if respect_gitignore and target.is_symlink() and _is_ignored_single_path(target):
                    return
                for child in target.iterdir():
                    _expand_target(child, is_direct=False)
            else:
                expanded.append(target)
                if is_direct:
                    directly_specified.add(target)
        except OSError:
            pyfltr.warnings_.emit_warning(
                source="file-resolver",
                message=f"I/O Error: {target}",
                exc_info=True,
            )

    for target in targets:
        # 絶対パスの場合はcwd基準の相対パスに変換
        if target.is_absolute():
            with contextlib.suppress(ValueError):
                target = target.relative_to(pathlib.Path.cwd())
        # 非存在パスは前段で検出して対象から外す。
        # 各ツールが個別に「ファイルが見つからない」と失敗してJSONLが多重化するのを防ぐ。
        # exclude/.gitignore除外（reason="excluded"）とは別系統（reason="missing"）で蓄積し、
        # CLI側では「全件不在 → 非ゼロ終了」の判定にだけ用いる。
        if not target.exists():
            pyfltr.warnings_.emit_warning(
                source="file-resolver",
                message=f"指定されたパスが見つかりません: {target}",
            )
            pyfltr.warnings_.add_filtered_direct_file(str(target), reason="missing")
            continue
        is_direct = not target.is_dir()
        _expand_target(target, is_direct=is_direct)

    # .gitignoreフィルタリング
    if respect_gitignore:
        before_gitignore = set(expanded)
        expanded = _filter_by_gitignore(expanded)
        # 直接指定されたファイルがgitignoreで除外された場合に警告
        after_set = set(expanded)
        for target in directly_specified:
            if target in before_gitignore and target not in after_set:
                pyfltr.warnings_.emit_warning(
                    source="file-resolver",
                    message=f"指定されたファイルが .gitignore により無視されました: {target}",
                )
                pyfltr.warnings_.add_filtered_direct_file(str(target), reason="excluded")

    return _dedup_and_sort(expanded)


def _dedup_and_sort(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """実体パス単位で重複排除し、パス文字列で安定ソートして返す。

    同一実体に複数パスが紐付く場合は非シンボリックリンクのパスを優先して残す。
    prettier等の一部ツールは末端がシンボリックリンクのファイルを明示指定されると
    `Explicitly specified pattern "X" is a symbolic link`エラーで失敗する。
    `iterdir()`の返却順はOS依存（Linuxのext4は作成順・WindowsのNTFSはアルファベット順）で
    入力順が揺れるため、非シンボリックリンク優先の選定基準で揺れを吸収し決定論的にエラーを回避する。
    シンボリックリンクを辿った結果、同一実体ファイルが複数パスから列挙される構成
    （例: `.agents/skills` → `.claude/skills`）でツールチェックを多重実行するのを避ける目的も兼ねる。
    同一実体内で全パスがシンボリックリンクか／全パスが非シンボリックリンクの同点時は、
    パス文字列で安定化する。
    """
    candidates: dict[pathlib.Path, list[pathlib.Path]] = {}
    for p in paths:
        try:
            real = p.resolve()
        except OSError:
            real = p.absolute()
        candidates.setdefault(real, []).append(p)
    deduped = [min(group, key=_dedup_selection_key) for group in candidates.values()]
    deduped.sort(key=str)
    return deduped


def _dedup_selection_key(path: pathlib.Path) -> tuple[bool, str]:
    """`_dedup_and_sort`の選定キー。非シンボリックリンクを優先し、同点はパス文字列で安定化する。

    `is_symlink()`のOSErrorはsymlink扱いに分類し、非シンボリックリンクを最大限残す。
    """
    try:
        is_link = path.is_symlink()
    except OSError:
        is_link = True
    return (is_link, str(path))


def _is_ignored_single_path(path: pathlib.Path) -> bool:
    """単一パスのgitignore判定。

    末尾`/`を付けない指定で`git check-ignore`に問い合わせる。
    `.gitignore`のファイル形式パターン（`name`・`*pattern*`等）に一致する場合のみ
    Trueを返す。`link/`形式のディレクトリ専用パターンには一致しない。
    git不在・タイムアウト・その他エラー時はFalseを返し、呼び出し元は通常走査を続ける。
    """
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", str(path)],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _filter_by_gitignore(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """Git check-ignoreで .gitignoreに該当するファイルを除外する。

    各入力パスを実体パスに正規化し、cwdリポジトリからの相対パスへ変換した上で
    `git check-ignore --stdin -z`に渡す。シンボリックリンク越えのpathspecで
    `fatal: pathspec ... is beyond a symbolic link`が発生してreturncode 128となり
    .gitignore除外処理が丸ごとスキップされる事象を回避するため。
    cwdリポジトリ外に解決されたパスは判定対象外として、入力パスをそのまま残す。

    サブプロセスのreturncodeが0でも1でもない場合は`emit_warning`でstderrを通知する。
    `logger.debug`のみで全パスを素通しさせるサイレントなフォールバックは
    .gitignore除外スキップを覆い隠す不具合の温床となるため採用しない。
    stdoutで返ってきた部分結果はignored判定として活用する。
    """
    if not paths:
        return paths
    cwd_real = pathlib.Path.cwd().resolve()
    in_repo: list[tuple[pathlib.Path, str]] = []  # (元path, cwd相対化したrealpath str)
    out_repo: list[pathlib.Path] = []  # cwd外（gitignore判定対象外）
    for p in paths:
        try:
            real = p.resolve()
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
        )
    except FileNotFoundError:
        pyfltr.warnings_.emit_warning(source="git", message="git が見つからないため respect-gitignore をスキップする")
        return paths
    except subprocess.TimeoutExpired:
        pyfltr.warnings_.emit_warning(source="git", message="git check-ignore がタイムアウトしたためスキップする")
        return paths
    if result.returncode not in (0, 1):
        # 0: 1つ以上ignored, 1: 全てnot ignored, 128等: fatal error。
        # サイレント素通しは.gitignore除外スキップを覆い隠すため emit_warning で通知し、
        # stdoutで返ってきた部分結果はignored判定として活用する。
        stderr_msg = result.stderr.strip() if result.stderr else ""
        detail = f": {stderr_msg}" if stderr_msg else ""
        pyfltr.warnings_.emit_warning(
            source="git",
            message=(
                f"git check-ignore が終了コード {result.returncode} を返したため "
                f"一部の .gitignore 判定がスキップされました{detail}"
            ),
        )
    ignored_set: set[str] = set()
    if result.stdout:
        ignored_set = {s for s in result.stdout.split("\0") if s}
    not_ignored_in_repo = [p for p, rel in in_repo if rel not in ignored_set]
    return not_ignored_in_repo + out_repo


def filter_by_globs(all_files: list[pathlib.Path], globs: list[str]) -> list[pathlib.Path]:
    """ファイルリストをglobパターンでフィルタリングする。"""
    return [f for f in all_files if any(f.match(glob) for glob in globs)]


def filter_by_changed_since(all_files: list[pathlib.Path], ref: str) -> list[pathlib.Path]:
    """`--changed-since <ref>` で変更ファイルにフィルタリングする。

    `git diff --name-only <ref>` でコミット差分とtrackedファイルの作業ツリー差分・staged差分の
    和集合を取得し、`all_files` との交差を返す。
    untracked（`git add` 未実施の新規ファイル）は対象外となる。

    git不在またはrefが存在しない場合は警告を発行して `all_files` をそのまま返す（全体実行へフォールバック）。
    """
    changed = _get_changed_files(ref)
    if changed is None:
        return all_files
    if not changed:
        return []
    # normalize_separatorsを使って区切り文字を統一してから比較する。
    # all_filesはcwd起点の相対Pathであり、git diffもcwd起点の相対パスを返す。
    changed_norm: set[str] = {pyfltr.paths.normalize_separators(p) for p in changed}
    return [f for f in all_files if pyfltr.paths.normalize_separators(f) in changed_norm]


def _get_changed_files(ref: str) -> list[str] | None:
    """`git diff --name-only <ref>` でコミット差分とtrackedファイルの作業ツリー差分・staged差分を取得する。

    untracked（`git add` 未実施の新規ファイル）は `git diff` の出力に含まれないため対象外となる。
    成功時はパス文字列のリストを返す。git不在・ref不在・タイムアウト時は
    警告を発行して `None` を返す（呼び出し元が全体実行へフォールバックする）。
    """
    # HEADからのコミット差分（<ref>..HEAD）とtrackedファイルの作業ツリー差分・staged差分の
    # 3種を「git diff <ref>」1コマンドで取得する。
    # git diff <ref> は <ref> と作業ツリーの差分（stagedも含む）を返すため、
    # コミット間差分 + trackedファイルの作業ツリー差分を一度に網羅できる。
    # 出力は-zオプションでNUL区切りにしてパスにスペースや特殊文字が含まれるケースに対応する。
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "-z", ref],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"git が見つからないため --changed-since={ref!r} をスキップして全体実行します",
        )
        return None
    except subprocess.TimeoutExpired:
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"git diff --name-only がタイムアウトしたため --changed-since={ref!r} をスキップして全体実行します",
        )
        return None
    if result.returncode != 0:
        # 終了コード非0はref不在・リポジトリ外などを示す。
        stderr_msg = result.stderr.strip()
        detail = f": {stderr_msg}" if stderr_msg else ""
        pyfltr.warnings_.emit_warning(
            source="changed-since",
            message=f"--changed-since={ref!r} の ref が解決できないためスキップして全体実行します{detail}",
        )
        return None
    # NUL区切りでパースし、空文字列エントリを除去する。
    return [p for p in result.stdout.split("\0") if p]


def matches_exclude_patterns(path: pathlib.Path, patterns: list[str]) -> str | None:
    """パスが除外パターンのいずれかに一致した場合、最初に一致したパターン文字列を返す。"""
    for glob in patterns:
        if path.match(glob):
            return glob
    # 親ディレクトリに一致しても可
    part = path.parent
    for _ in range(len(path.parts) - 1):
        for glob in patterns:
            if part.match(glob):
                return glob
        part = part.parent
    return None


def excluded(path: pathlib.Path, config: pyfltr.config.config.Config) -> tuple[str, str] | None:
    """無視パターンチェック。一致した場合は（設定キー名, 一致パターン）を、無一致の場合はNoneを返す。"""
    for key in ("exclude", "extend-exclude"):
        matched = matches_exclude_patterns(path, config[key])
        if matched is not None:
            return (key, matched)
    return None
