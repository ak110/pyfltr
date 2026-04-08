"""コマンド実行関連の処理。"""

import argparse
import atexit
import contextlib
import dataclasses
import logging
import os
import pathlib
import random
import shlex
import shutil
import subprocess
import time
import typing

import natsort

import pyfltr.config
import pyfltr.error_parser

logger = logging.getLogger(__name__)

_active_processes: list[subprocess.Popen] = []  # type: ignore[type-arg]


def _cleanup_processes() -> None:
    """プロセス終了時に実行中の子プロセスを終了。"""
    for proc in _active_processes:
        with contextlib.suppress(OSError):
            proc.kill()


atexit.register(_cleanup_processes)


@dataclasses.dataclass
class CommandResult:
    """コマンドの実行結果。"""

    command: str
    command_type: str
    commandline: list[str]
    returncode: int | None
    has_error: bool
    files: int
    output: str
    elapsed: float
    errors: list[pyfltr.error_parser.ErrorLocation] = dataclasses.field(default_factory=list)

    @property
    def alerted(self) -> bool:
        """skipped/succeeded以外ならTrue"""
        return self.returncode is not None and self.returncode != 0

    @property
    def status(self) -> str:
        """ステータスの文字列を返す。"""
        if self.returncode is None:
            status = "skipped"
        elif self.returncode == 0:
            status = "succeeded"
        elif self.command_type == "formatter" and not self.has_error:
            status = "formatted"
        else:
            status = "failed"
        return status

    def get_status_text(self) -> str:
        """成型した文字列を返す。"""
        return f"{self.status} ({self.files}files in {self.elapsed:.1f}s)"


def _run_subprocess(
    commandline: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """サブプロセスの実行。"""
    if on_output is None:
        return subprocess.run(
            commandline,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
        )
    with subprocess.Popen(
        commandline,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
    ) as proc:
        _active_processes.append(proc)
        try:
            output_lines: list[str] = []
            assert proc.stdout is not None
            for line in proc.stdout:
                output_lines.append(line)
                on_output(line)
            proc.wait()
            return subprocess.CompletedProcess(
                args=commandline,
                returncode=proc.returncode,
                stdout="".join(output_lines),
            )
        finally:
            _active_processes.remove(proc)


def execute_command(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    on_output: typing.Callable[[str], None] | None = None,
) -> CommandResult:
    """コマンドの実行。"""
    command_info = config.commands[command]
    globs = [command_info.targets]
    targets: list[pathlib.Path] = expand_globs(args.targets, globs, config)

    # ファイルの順番をシャッフルまたはソート
    if args.shuffle:
        random.shuffle(targets)
    else:
        # natsort.natsorted の型ヒントが不十分で ty が union 型へ縮めるため cast で明示。
        targets = typing.cast("list[pathlib.Path]", natsort.natsorted(targets, key=str))

    commandline: list[str] = [config[f"{command}-path"]]
    commandline.extend(config[f"{command}-args"])

    # 起動オプションからの追加引数を適用
    additional_args_str = getattr(args, f"{command.replace('-', '_')}_args", "")
    if additional_args_str:
        additional_args = shlex.split(additional_args_str)
        commandline.extend(additional_args)

    commandline.extend(str(t) for t in targets)

    if len(targets) <= 0:
        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=commandline,
            returncode=None,
            has_error=False,
            output="対象ファイルが見つかりません。",
            files=0,
            elapsed=0,
        )

    start_time = time.perf_counter()
    env = _build_subprocess_env(config, command)

    # ruff-formatで ruff-format-by-check が有効な場合は、
    # 先に ruff check --fix --unsafe-fixes を実行してから ruff format を実行する。
    # ステップ1(check)の lint violation (exit 1) は無視する (lint は ruff-check で検出)。
    # ただし exit >= 2 (設定エラー等) は失敗扱いする。
    if command == "ruff-format" and config["ruff-format-by-check"]:
        result = _execute_ruff_format_two_step(
            command, command_info, commandline, targets, config, args, env, on_output, start_time
        )
        return result

    # --checkオプションを使わないとファイル変更があったかわからないコマンドは、
    # 一度--checkオプションをつけて実行してから、
    # 変更があった場合は再度--checkオプションなしで実行する。
    check_args = ["--check"] if command in ("autoflake", "isort", "black") else []

    has_error = False

    # verbose時はコマンドラインをon_output経由で出力
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline + check_args)}\n")
    proc = _run_subprocess(commandline + check_args, env, on_output)
    returncode = proc.returncode

    # autoflake/isort/blackの再実行
    if returncode != 0 and command in ("autoflake", "isort", "black"):
        if args.verbose and on_output is not None:
            on_output(f"commandline: {shlex.join(commandline)}\n")
        proc = _run_subprocess(commandline, env, on_output)
        if proc.returncode != 0:
            returncode = proc.returncode
            has_error = True

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    # エラー箇所のパース
    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)

    return CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )


def _build_subprocess_env(config: pyfltr.config.Config, command: str) -> dict[str, str]:
    """サブプロセス実行用の環境変数を構築。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if config.values.get(f"{command}-devmode", False):
        env["PYTHONDEVMODE"] = "1"
    # 表示幅を適切な範囲に制限する
    # (pytestなどは一部の表示が右寄せになるのであまり大きいと見づらい)
    env["COLUMNS"] = str(min(max(shutil.get_terminal_size().columns - 4, 80), 128))
    return env


def _execute_ruff_format_two_step(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    format_commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.Config,
    args: argparse.Namespace,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
) -> CommandResult:
    """ruff-format の 2 段階実行 (ruff check --fix → ruff format)。

    ステップ1 (ruff check --fix --unsafe-fixes) の未修正 lint violation は無視する。
    別途 ruff-check コマンドで検出される前提。ただし exit >= 2 (設定ミス等) は failed 扱い。
    ステップ1 の成否にかかわらずステップ2 (ruff format) は実行する
    (対象ファイル全体の format 適用を止めないため)。
    """
    # ステップ1のコマンドライン組立
    check_commandline: list[str] = [config["ruff-format-path"]]
    check_commandline.extend(config["ruff-format-check-args"])
    check_commandline.extend(str(t) for t in targets)

    # ステップ1実行前の mtime を記録 (修正適用検知用)
    mtimes_before = _snapshot_mtimes(targets)

    # ステップ1実行
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = _run_subprocess(check_commandline, env, on_output)
    step1_rc = step1_proc.returncode
    step1_failed = step1_rc >= 2  # exit 0/1 は無視、2 以上 (abrupt termination) のみ失敗扱い
    step1_changed = _snapshot_mtimes(targets) != mtimes_before

    # ステップ2実行 (常に実行)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(format_commandline)}\n")
    step2_proc = _run_subprocess(format_commandline, env, on_output)
    step2_rc = step2_proc.returncode
    step2_formatted = step2_rc == 1
    step2_failed = step2_rc >= 2

    # 出力の合成
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    # 最終判定
    has_error = step1_failed or step2_failed
    if has_error:
        returncode = step1_rc if step1_failed else step2_rc
    elif step1_changed or step2_formatted:
        returncode = 1
    else:
        returncode = 0

    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)

    # commandline は代表として「最後に実行したステップ」(= ruff format) を格納。
    # 両ステップ分の commandline は verbose 出力で確認可能。
    return CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=format_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )


def _snapshot_mtimes(targets: list[pathlib.Path]) -> dict[pathlib.Path, int]:
    """対象ファイルの mtime (ns) スナップショットを取得。

    ファイルが存在しない場合は -1 を設定し、存在しないものとして扱う (比較で差分検知できる)。
    """
    result: dict[pathlib.Path, int] = {}
    for target in targets:
        try:
            result[target] = target.stat().st_mtime_ns
        except OSError:
            result[target] = -1
    return result


def expand_globs(targets: list[pathlib.Path], globs: list[str], config: pyfltr.config.Config) -> list[pathlib.Path]:
    """対象ファイルのリストアップ。"""
    # 空ならカレントディレクトリを対象とする
    if len(targets) == 0:
        targets = [pathlib.Path(".")]

    expanded: list[pathlib.Path] = []

    def _expand_target(target):
        try:
            if excluded(target, config):
                pass
            elif target.is_dir():
                # ディレクトリの場合、再帰
                for child in target.iterdir():
                    _expand_target(child)
            else:
                # ファイルの場合、globsのいずれかに一致するなら追加
                if any(target.match(glob) for glob in globs):
                    expanded.append(target)
        except OSError:
            logger.warning(f"I/O Error: {target}", exc_info=True)

    for target in targets:
        # 絶対パスの場合はcwd基準の相対パスに変換
        if target.is_absolute():
            with contextlib.suppress(ValueError):
                target = target.relative_to(pathlib.Path.cwd())
        _expand_target(target)

    return expanded


def excluded(path: pathlib.Path, config: pyfltr.config.Config) -> bool:
    """無視パターンチェック。"""
    excludes = config["exclude"] + config["extend-exclude"]
    # 対象パスに一致したらTrue
    if any(path.match(glob) for glob in excludes):
        return True
    # 親に一致してもTrue
    part = path.parent
    for _ in range(len(path.parts) - 1):
        if any(part.match(glob) for glob in excludes):
            return True
        part = part.parent
    # どれにも一致しなかったらFalse
    return False
