"""ツールの表示版と最低版要件の確認。"""

import functools
import pathlib
import re

import pyfltr.command.process
import pyfltr.command.runner
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command.env import build_subprocess_env

# pyfltrのコマンド名から、期待する機能が成立する最低版と要件の理由を引く。
# 版の選択はパッケージマネージャーへ委ねる既存方針を維持し、機能成立の検証だけを担う。
# 閾値はサブコマンドが追加された版ではなく、期待する観測結果が得られる最初の版とする。
# uv auditは0.10.8で追加され0.10.10で検出を開始したが、終了コードへ反映するのは0.11.2以降である。
PACKAGE_MANAGER_MIN_VERSION = {
    command: info.minimum_version for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.minimum_version is not None
}


# 版らしいトークンの判定。先頭の`v`を任意で許し、ドット区切りの数値が2要素以上続く形を版とみなす。
# ビルドメタデータ（`+build`）やプレリリース（`-rc1`）は数値要素の途中で一致が終わるため除外される。
_VERSION_TOKEN_PATTERN = re.compile(r"^v?(\d+(?:\.\d+)+)")

# `--version`の応答を待つ上限秒数。応答しない実行ファイルでpyfltr全体が停止するのを防ぐ。
# 超過時はreturncodeが非0となり、版を判別できない場合と同じ拒否へ倒れる。
_VERSION_PROBE_TIMEOUT = 30.0


def _extract_tool_version(output: str) -> tuple[int, ...] | None:
    """`--version`の出力から数値版を取り出す。

    ツールごとに`uv 0.11.7 (...)`・`pnpm 10.2.0`・`2026.7.1 linux-x64`と形が異なり、
    警告行が版行より先に出る実装もある。行頭固定・2トークン目固定では取りこぼすため、
    全行を順に走査して最初に見つかった版らしいトークンを採用する。
    `0.10.10+build`のような付随情報は数値要素のみへ切り詰める。
    見つからない場合は`None`を返し、呼び出し側が検証不能として扱えるようにする。
    """
    for line in output.splitlines():
        for token in line.split():
            matched = _VERSION_TOKEN_PATTERN.match(token)
            if matched is not None:
                return tuple(int(n) for n in matched.group(1).split("."))
    return None


def _format_version(version: tuple[int, ...]) -> str:
    """数値版をドット区切りの文字列へ整形する。"""
    return ".".join(str(n) for n in version)


@functools.lru_cache(maxsize=1)
def _get_tool_version(
    commandline: tuple[str, ...], env_items: tuple[tuple[str, str], ...], cwd: pathlib.Path | None
) -> tuple[int, ...] | None:
    """解決済みコマンドラインへ`--version`を渡して数値版を取得する。

    `commandline`は`ResolvedCommandline.commandline`（`[executable, *prefix]`）を渡す。
    実行ファイルだけを渡すと、mise等のラッパー経由で解決された場合にラッパー自身の版を
    測ってしまうため、起動と同じ形へ`--version`を付けて問い合わせる。
    取得または解釈に失敗した場合は`None`を返す。
    """
    proc = pyfltr.command.process.run_process_loop(
        [*commandline, "--version"], dict(env_items), cwd=cwd, timeout=_VERSION_PROBE_TIMEOUT
    )
    if proc.returncode != 0:
        return None
    return _extract_tool_version(proc.stdout)


def probe_tool_version_text(
    resolved: pyfltr.command.runner.ResolvedCommandline,
    config: pyfltr.config.model.Config,
    command: str,
    *,
    cwd: pathlib.Path | None = None,
) -> str | None:
    """解決済みコマンドラインへ`--version`を渡して版文字列をそのまま取得する。

    `command-info --check`が実行版を表示するために用いる。`_get_tool_version`は
    数値タプルへ変換して付随情報を失うため、表示用途にはこちらを使う。
    `cargo-fmt`などのサブコマンド型ツールでは基底ツールの版となる。
    採用するのは統合済み標準出力のうち数値版を含む最初の非警告行とする。
    数値版を含む非警告行がない場合は`None`を返す。
    非0終了・標準出力が空・起動失敗のいずれでも`None`を返す。

    envは`build_subprocess_env`へ実効runnerを渡して構築する。
    PATH上にmiseのtoolエントリが見えているとmiseがtools解決をスキップしてPATH解決へ
    フォールバックし、指定版と異なる実行ファイルの版を測るためである。
    uv経路では`VIRTUAL_ENV`の除外も同じ環境生成で行い、実ツール起動と同じ環境で版を測る。
    """
    env = build_subprocess_env(
        config,
        command,
        via_mise=resolved.effective_runner == "mise",
        effective_runner=resolved.effective_runner,
    )
    try:
        proc = pyfltr.command.process.run_process_loop(
            [*resolved.commandline, "--version"], env, cwd=cwd, timeout=_VERSION_PROBE_TIMEOUT
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return _preferred_version_line(proc.stdout)


def _preferred_version_line(output: str) -> str | None:
    """数値版を含む最初の非警告行を返す。"""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or stripped.casefold().startswith("warning"):
            continue
        if any(_VERSION_TOKEN_PATTERN.match(token) is not None for token in stripped.split()):
            return stripped
    return None


def ensure_package_manager_version(
    resolved: pyfltr.command.runner.ResolvedCommandline,
    config: pyfltr.config.model.Config,
    command: str,
    *,
    cwd: pathlib.Path | None = None,
) -> None:
    """パッケージマネージャー系コマンドの最低版要件を確認する。"""
    requirement = PACKAGE_MANAGER_MIN_VERSION.get(command)
    if requirement is None:
        return
    minimum, reason = requirement
    minimum_text = _format_version(minimum)
    env = build_subprocess_env(config, command, effective_runner=resolved.effective_runner)
    version = _get_tool_version(tuple(resolved.commandline), tuple(sorted(env.items())), cwd)
    shown = " ".join(resolved.commandline)
    hint = f"`{command}-path`で要件を満たす実行ファイルを指定することもできます。"
    if version is None:
        raise ValueError(
            f"{shown}のバージョンを判別できませんでした。{command}には{minimum_text}以降が必要です。{reason}。{hint}"
        )
    if version < minimum:
        version_text = _format_version(version)
        raise ValueError(
            f"{shown}のバージョンは{version_text}です。{command}には{minimum_text}以降が必要です。{reason}。{hint}"
        )
