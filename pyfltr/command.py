"""コマンド実行関連の処理。"""
# pylint: disable=too-many-lines

import argparse
import atexit
import contextlib
import dataclasses
import hashlib
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


# pyfltr のコマンド名 -> 実際に起動するパッケージの bin 名の対応表。
# markdownlint コマンドは実体が markdownlint-cli2 である点に注意。
_JS_TOOL_BIN: dict[str, str] = {
    "textlint": "textlint",
    "markdownlint": "markdownlint-cli2",
    "eslint": "eslint",
    "prettier": "prettier",
    "biome": "biome",
    "vitest": "vitest",
    "oxlint": "oxlint",
    "tsc": "tsc",
}

# pnpx 経由で解決するときに `--package` に渡す spec。
# 通常は bin 名をそのまま渡すだけだが、上流の既知バグで動かないバージョンを
# 除外したい場合やスコープ付きパッケージの場合にここで差し替える。
# - textlint 15.5.3 には起動不能のバグがあるため除外している (15.5.4 で修正済み)。
# - biome は bin 名が "biome" だが npm パッケージは "@biomejs/biome" (スコープ付き)。
_JS_TOOL_PNPX_PACKAGE_SPEC: dict[str, str] = {
    "textlint": "textlint@<15.5.3 || >15.5.3",
    "biome": "@biomejs/biome",
    "oxlint": "oxlint",
    "tsc": "typescript",  # tsc コマンドは typescript パッケージに含まれる
}


@dataclasses.dataclass(frozen=True)
class BinToolSpec:
    """bin-runner対応ツールの解決情報。"""

    bin_name: str
    """実行ファイル名"""
    mise_backend: str | None = None
    """mise exec用のbackend指定（省略時はbin_name）"""
    default_version: str = "latest"
    """既定バージョン"""


# bin-runner で解決するネイティブバイナリツールの定義。
# path 設定が空のとき、bin-runner 設定に基づいてコマンドを組み立てる。
_BIN_TOOL_SPEC: dict[str, BinToolSpec] = {
    "editorconfig-checker": BinToolSpec(bin_name="editorconfig-checker"),
    "shellcheck": BinToolSpec(bin_name="shellcheck"),
    "shfmt": BinToolSpec(bin_name="shfmt"),
    "typos": BinToolSpec(bin_name="typos"),
    "actionlint": BinToolSpec(bin_name="actionlint"),
}


def _resolve_bin_commandline(
    command: str,
    config: pyfltr.config.Config,
) -> tuple[str, list[str]]:
    """ネイティブバイナリツールの実行ファイルと引数 prefix を決定する。

    `{command}-path` が空のときに呼び出され、`bin-runner` 設定に基づいて
    起動コマンドを組み立てる。ツールが利用できない場合は `FileNotFoundError` を送出する。
    """
    spec = _BIN_TOOL_SPEC[command]
    runner = config["bin-runner"]
    version = config.values.get(f"{command}-version", spec.default_version)

    if runner == "direct":
        resolved = shutil.which(spec.bin_name)
        if resolved is None:
            raise FileNotFoundError(spec.bin_name)
        return resolved, []

    if runner == "mise":
        if shutil.which("mise") is None:
            raise FileNotFoundError("mise")
        tool_name = spec.mise_backend or spec.bin_name
        tool_spec = f"{tool_name}@{version}"
        # バージョン指定込みでツールの利用可否を事前チェック
        check = subprocess.run(
            ["mise", "exec", tool_spec, "--", spec.bin_name, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if check.returncode != 0:
            raise FileNotFoundError(f"mise exec {tool_spec} -- {spec.bin_name}")
        return "mise", ["exec", tool_spec, "--", spec.bin_name]

    raise ValueError(f"bin-runnerの設定値が正しくありません: {runner=}")


def _skipped_bin_resolution_result(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    error: FileNotFoundError,
) -> "CommandResult":
    """Bin ツールの解決失敗時のスキップ用 `CommandResult` を組み立てる。"""
    message = f"ツールが見つかりません（スキップ）: {error}"
    logger.warning("%s: %s", command, message)
    return CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=[],
        returncode=None,
        has_error=False,
        files=0,
        output=message,
        elapsed=0.0,
    )


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


def _resolve_js_commandline(
    command: str,
    config: pyfltr.config.Config,
) -> tuple[str, list[str]]:
    """JS ツール (textlint / markdownlint) の実行ファイルと引数 prefix を決定する。

    `{command}-path` が空のときに呼び出され、`js-runner` 設定に基づいて
    起動コマンドを組み立てる。`direct` モードで `node_modules/.bin/<cmd>` が
    存在しない場合は `FileNotFoundError` を送出する。
    """
    bin_name = _JS_TOOL_BIN[command]
    runner = config["js-runner"]
    # 汎用化: `{command}-packages` キーを参照することで任意の JS ツールで
    # `--package` / `-p` 展開を利用可能にする。未定義キーは空リスト扱い。
    packages: list[str] = list(config.values.get(f"{command}-packages", []))

    if runner == "pnpx":
        main_spec = _JS_TOOL_PNPX_PACKAGE_SPEC.get(command, bin_name)
        prefix: list[str] = ["--package", main_spec]
        for pkg in packages:
            prefix.extend(["--package", pkg])
        prefix.append(bin_name)
        return "pnpx", prefix
    if runner == "pnpm":
        return "pnpm", ["exec", bin_name]
    if runner == "npm":
        return "npm", ["exec", "--no", "--", bin_name]
    if runner == "npx":
        prefix = ["--no-install"]
        for pkg in packages:
            prefix.extend(["-p", pkg])
        prefix.extend(["--", bin_name])
        return "npx", prefix
    if runner == "yarn":
        return "yarn", ["run", bin_name]
    if runner == "direct":
        bin_dir = pathlib.Path("node_modules") / ".bin"
        # Windows では `.cmd` 付きのラッパーを優先する。pyright の静的評価では
        # Linux 上だと `sys.platform == "win32"` 側の分岐を unreachable とみなすため、
        # `os.name` を経由して静的分岐とみなされないようにする。
        candidates: list[pathlib.Path] = []
        if os.name == "nt":
            candidates.append(bin_dir / f"{bin_name}.cmd")
        candidates.append(bin_dir / bin_name)
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate), []
        raise FileNotFoundError(str(candidates[0]))
    raise ValueError(f"js-runnerの設定値が正しくありません: {runner=}")


def _failed_js_resolution_result(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    error: FileNotFoundError,
) -> "CommandResult":
    """Js ツールの解決失敗時の `CommandResult` を組み立てる。"""
    message = (
        f"js-runner=direct 指定ですが実行ファイルが見つかりません: {error}. "
        "package.jsonで対象パッケージをインストールしてください。"
    )
    return CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=[],
        returncode=1,
        has_error=True,
        files=0,
        output=message,
        elapsed=0.0,
    )


def _run_subprocess(
    commandline: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """サブプロセスの実行。

    実行ファイルが見つからない場合は `FileNotFoundError` を握りつぶし、
    rc=127 の `CompletedProcess` として返す。これにより並列実行下の他コマンドを
    巻き込まずに、呼び出し側で通常の失敗として扱える。
    """
    try:
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
    except FileNotFoundError as e:
        message = f"実行ファイルが見つかりません: {commandline[0]} ({e})\n"
        if on_output is not None:
            on_output(message)
        return subprocess.CompletedProcess(
            args=commandline,
            returncode=127,
            stdout=message,
        )


def execute_command(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    on_output: typing.Callable[[str], None] | None = None,
) -> CommandResult:
    """コマンドの実行。"""
    command_info = config.commands[command]
    globs = command_info.target_globs()
    targets: list[pathlib.Path] = expand_globs(args.targets, globs, config)

    # ファイルの順番をシャッフルまたはソート
    if args.shuffle:
        random.shuffle(targets)
    else:
        # natsort.natsorted の型ヒントが不十分で ty が union 型へ縮めるため cast で明示。
        targets = typing.cast("list[pathlib.Path]", natsort.natsorted(targets, key=str))

    # fix モード判定: `pyfltr --fix` かつ当該コマンドに fix-args が定義されている場合は、
    # linter 向けの単発 fix 実行経路を使う。fix-args 未定義の formatter は通常経路を通る
    # (通常実行そのものがファイルを修正するため fix モードでも挙動は変わらない)。
    fix_mode = bool(getattr(args, "fix", False))
    fix_args: list[str] | None = None
    if fix_mode:
        fix_args = config.values.get(f"{command}-fix-args")

    # textlint / markdownlint は path が空の場合、js-runner 設定から解決する。
    # editorconfig-checker 等は bin-runner 設定から解決する。
    if command in _JS_TOOL_BIN and config[f"{command}-path"] == "":
        try:
            resolved_path, prefix = _resolve_js_commandline(command, config)
        except FileNotFoundError as e:
            return _failed_js_resolution_result(command, command_info, e)
        commandline_prefix: list[str] = [resolved_path, *prefix]
    elif command in _BIN_TOOL_SPEC and config[f"{command}-path"] == "":
        try:
            resolved_path, prefix = _resolve_bin_commandline(command, config)
        except FileNotFoundError as e:
            return _skipped_bin_resolution_result(command, command_info, e)
        commandline_prefix = [resolved_path, *prefix]
    else:
        commandline_prefix = [config[f"{command}-path"]]

    # 起動オプションからの追加引数 (--textlint-args など) を shlex 分割しておく
    additional_args_str = getattr(args, f"{command.replace('-', '_')}_args", "")
    additional_args = shlex.split(additional_args_str) if additional_args_str else []

    # commandline を組み立てる:
    #   [prefix] + [auto-args] + args + (lint-args or fix-args) + additional_args + targets
    # auto-args: AUTO_ARGS で定義された自動引数（フラグが True かつ重複なしの場合に挿入）
    # lint-args (非 fix モードでのみ付与される引数) は textlint の --format compact のように、
    # lint 実行時にのみ必要なオプションを分離するためのキー。
    user_args: list[str] = config[f"{command}-args"]
    auto_args = _build_auto_args(command, config, user_args + additional_args)
    commandline: list[str] = [*commandline_prefix, *auto_args, *user_args]
    if fix_args is not None:
        commandline.extend(fix_args)
    else:
        commandline.extend(config.values.get(f"{command}-lint-args", []))
    commandline.extend(additional_args)
    # pass-filenames = false のツールはファイル引数を渡さない（tsc 等）
    if config.values.get(f"{command}-pass-filenames", True):
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

    # textlint の fix モードは 2 段階実行 (fix 適用 + lint チェック)。
    # fixer-formatter が compact をサポートしない問題と、残存違反を compact で取得する
    # 要件を両立させるため、他の linter とは別経路で実行する。
    if fix_args is not None and command == "textlint":
        return _execute_textlint_fix(
            command,
            command_info,
            commandline_prefix,
            config,
            targets,
            additional_args,
            env,
            on_output,
            start_time,
            args,
        )

    # fix モードで linter に fix-args を適用する経路。
    # mtime 変化で formatted 判定を行い、rc != 0 はそのまま failed 扱いとする。
    if fix_args is not None and command_info.type != "formatter":
        return _execute_linter_fix(command, command_info, commandline, targets, env, on_output, start_time, args)

    # ruff-formatで ruff-format-by-check が有効な場合は、
    # 先に ruff check --fix --unsafe-fixes を実行してから ruff format を実行する。
    # ステップ1(check)の lint violation (exit 1) は無視する (lint は ruff-check で検出)。
    # ただし exit >= 2 (設定エラー等) は失敗扱いする。
    if command == "ruff-format" and config["ruff-format-by-check"]:
        result = _execute_ruff_format_two_step(
            command, command_info, commandline, targets, config, args, env, on_output, start_time
        )
        return result

    # shfmt は -l (確認) と -w (書き込み) が排他のため prettier 同様の 2 段階実行。
    if command == "shfmt":
        return _execute_shfmt_two_step(
            command,
            command_info,
            commandline_prefix,
            config,
            targets,
            additional_args,
            fix_mode=fix_mode,
            env=env,
            on_output=on_output,
            start_time=start_time,
            args=args,
        )

    # prettier は --check (read-only) と --write (書き込み) が排他のため 2 段階実行する。
    # ruff-format と同じ位置・スタイルで分岐する。
    # prettier には {cmd}-fix-args を定義していないため fix 判定は args.fix 由来の
    # fix_mode 変数を使う (filter_fix_commands では formatter として常に fix 対象となる)。
    if command == "prettier":
        return _execute_prettier_two_step(
            command,
            command_info,
            commandline_prefix,
            config,
            targets,
            additional_args,
            fix_mode=fix_mode,
            env=env,
            on_output=on_output,
            start_time=start_time,
            args=args,
        )

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


def _build_auto_args(command: str, config: pyfltr.config.Config, user_args: list[str]) -> list[str]:
    """自動引数を構築する。

    AUTO_ARGS で定義されたフラグが True の場合、対応する引数を返す。
    ユーザーが *-args や CLI 引数で既に同じ文字列を指定している場合はスキップする。
    """
    auto_entries = pyfltr.config.AUTO_ARGS.get(command, [])
    if not auto_entries:
        return []
    user_args_joined = " ".join(user_args)
    result: list[str] = []
    for flag_key, args in auto_entries:
        if not config.values.get(flag_key, False):
            continue
        for arg in args:
            if arg not in user_args_joined:
                result.append(arg)
    return result


def _build_subprocess_env(config: pyfltr.config.Config, command: str) -> dict[str, str]:
    """サブプロセス実行用の環境変数を構築。"""
    env = os.environ.copy()
    # サプライチェーン攻撃対策: パッケージ取得系ツールの最小待機期間を既定で設定する。
    # ユーザーが既に設定している場合はその値を尊重する。
    # pnpm は npm 互換の config 環境変数方式 (NPM_CONFIG_<SNAKE_CASE>) を採る。
    env.setdefault("UV_EXCLUDE_NEWER", "1 day")
    env.setdefault("NPM_CONFIG_MINIMUM_RELEASE_AGE", "1440")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if config.values.get(f"{command}-devmode", False):
        env["PYTHONDEVMODE"] = "1"
    # 表示幅を適切な範囲に制限する
    # (pytestなどは一部の表示が右寄せになるのであまり大きいと見づらい)
    env["COLUMNS"] = str(min(max(shutil.get_terminal_size().columns - 4, 80), 128))
    return env


def _execute_linter_fix(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
) -> CommandResult:
    """Fix モードでの linter 実行 (fix-args を適用して単発実行)。

    ステータス判定:
    - returncode != 0 → failed (ファイル変化に関係なく、エラーを握りつぶさない)
    - returncode == 0 かつ内容ハッシュに変化あり → formatted (command_type を
      "formatter" に差し替えて既存の status プロパティに委ねる)
    - returncode == 0 かつ変化なし → succeeded

    ruff-check は残存違反があると rc=1 を返すが、この設計では failed として扱う。
    未修正の違反はユーザーが後段で認識すべき情報であり、成功に寄せない方針。
    """
    del command_info  # noqa  # 呼び出し側との引数形式揃え用 (使用しない)

    digests_before = _snapshot_file_digests(targets)

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
    proc = _run_subprocess(commandline, env, on_output)
    returncode = proc.returncode
    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    changed = _snapshot_file_digests(targets) != digests_before

    has_error = returncode != 0
    if not has_error and changed:
        # fix が適用されたので formatter 扱いで formatted にする
        result_command_type: str = "formatter"
        returncode = 1
    else:
        result_command_type = "linter"

    errors = pyfltr.error_parser.parse_errors(command, output, None)

    return CommandResult(
        command=command,
        command_type=result_command_type,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )


def _execute_textlint_fix(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
) -> CommandResult:
    """Textlint fix モードの 2 段階実行 (fix 適用 → lint チェック)。

    textlint は lint 実行と fix 実行でフォーマッタ解決に使うパッケージが異なり
    (`@textlint/linter-formatter` と `@textlint/fixer-formatter`)、fixer 側は
    `compact` フォーマッタをサポートしない。このため `textlint --format compact --fix`
    がクラッシュする。また `textlint --fix` の既定出力 (stylish) は本ツールの
    builtin パーサ (compact 前提) で解析できないため、残存違反を取得するには
    別途 lint 実行を行う必要がある。

    上記を両立させるため本関数では次の 2 段階を直列実行する。

    Step1: fix 適用
        commandline_prefix + (textlint-args から --format ペアを除去) + fix-args
        + additional_args + targets

    Step2: lint チェック (残存違反を compact 形式で取得)
        commandline_prefix + textlint-args + textlint-lint-args + additional_args + targets

    ステータス判定:
    - いずれかのステップが rc>=2 (致命的エラー) → failed
    - Step2 rc != 0 (残存違反あり) → failed (Errors タブに反映される)
    - Step2 rc == 0 かつ Step1 で内容ハッシュに変化あり → formatted
    - Step2 rc == 0 かつ変化なし → succeeded

    textlint --fix は残存違反がなくても対象ファイルを書き戻すことがあり、
    mtime ベースの比較では偽陽性になる。このため内容ハッシュ
    (`_snapshot_file_digests`) で比較している。
    """
    common_args: list[str] = list(config[f"{command}-args"])
    lint_args: list[str] = list(config.values.get(f"{command}-lint-args", []))
    fix_args: list[str] = list(config.values.get(f"{command}-fix-args", []))
    target_strs = [str(t) for t in targets]

    # Step1: --format X ペアを除去した共通 args + fix-args で fix 適用
    step1_common_args = _strip_format_option(common_args)
    step1_commandline: list[str] = [
        *commandline_prefix,
        *step1_common_args,
        *fix_args,
        *additional_args,
        *target_strs,
    ]

    digests_before = _snapshot_file_digests(targets)

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step1_commandline)}\n")
    step1_proc = _run_subprocess(step1_commandline, env, on_output)
    step1_rc = step1_proc.returncode
    # rc=0 (違反なし) / rc=1 (違反残存) は通常終了、rc>=2 は致命的エラー扱い
    step1_fatal = step1_rc >= 2
    step1_changed = _snapshot_file_digests(targets) != digests_before

    # Step2: 通常 lint 実行 (compact フォーマッタで残存違反を取得)
    step2_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *lint_args,
        *additional_args,
        *target_strs,
    ]

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step2_commandline)}\n")
    step2_proc = _run_subprocess(step2_commandline, env, on_output)
    step2_rc = step2_proc.returncode
    step2_fatal = step2_rc >= 2

    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    # Step2 出力 (compact 形式) から残存違反をパースする
    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)

    # ステータス判定
    if step1_fatal or step2_fatal:
        has_error = True
        returncode: int = step1_rc if step1_fatal else step2_rc
        result_command_type: str = "linter"
    elif step2_rc != 0:
        has_error = True
        returncode = step2_rc
        result_command_type = "linter"
    elif step1_changed:
        # fix 適用済み、残存違反なし → formatted 扱いにする
        has_error = False
        returncode = 1
        result_command_type = "formatter"
    else:
        has_error = False
        returncode = 0
        result_command_type = "linter"

    return CommandResult(
        command=command,
        command_type=result_command_type,
        commandline=step2_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )


def _strip_format_option(args: list[str]) -> list[str]:
    """引数列から `--format X` / `-f X` / `--format=X` を除去する (順序は保持)。

    textlint の fix 実行時に使用する。`@textlint/fixer-formatter` はリンター側と
    異なるフォーマッタセットを持つため、ユーザーが共通 args に `--format compact` 等を
    指定していてもクラッシュしないように一律で除去する。compact 文字列を特別扱いしないのは、
    `--format json` などの組み合わせに対しても安全に振る舞うため。
    """
    result: list[str] = []
    skip_next = False
    for arg in args:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--format", "-f"):
            skip_next = True
            continue
        if arg.startswith("--format="):
            continue
        result.append(arg)
    return result


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

    # ステップ1実行前の内容ハッシュを記録 (修正適用検知用)
    digests_before = _snapshot_file_digests(targets)

    # ステップ1実行
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = _run_subprocess(check_commandline, env, on_output)
    step1_rc = step1_proc.returncode
    step1_failed = step1_rc >= 2  # exit 0/1 は無視、2 以上 (abrupt termination) のみ失敗扱い
    step1_changed = _snapshot_file_digests(targets) != digests_before

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


def _execute_shfmt_two_step(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_mode: bool,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
) -> CommandResult:
    """Shfmt の 2 段階実行 (shfmt -l → shfmt -w)。

    prettier と同様、確認用引数 (-l) と書き込み用引数 (-w) が排他のため専用経路で処理する。

    通常モード (fix_mode=False):

    - Step1: `prefix + args + check-args + additional + targets` を実行
    - Step1 rc == 0 → succeeded (整形不要)
    - Step1 rc != 0 → Step2 `prefix + args + write-args + additional + targets` を実行
      - Step2 rc == 0 → formatted (整形成功)
      - Step2 rc != 0 → failed

    fix モード (fix_mode=True):

    - Step1 をスキップし、直接 write-args 付きで実行
    - 内容ハッシュスナップショットで書き込みを検知
    """
    common_args: list[str] = list(config[f"{command}-args"])
    check_args: list[str] = list(config[f"{command}-check-args"])
    write_args: list[str] = list(config[f"{command}-write-args"])
    target_strs = [str(t) for t in targets]

    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]

    if fix_mode:
        digests_before = _snapshot_file_digests(targets)
        if args.verbose and on_output is not None:
            on_output(f"commandline: {shlex.join(write_commandline)}\n")
        write_proc = _run_subprocess(write_commandline, env, on_output)
        write_rc = write_proc.returncode
        output = write_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        changed = _snapshot_file_digests(targets) != digests_before

        if write_rc != 0:
            has_error = True
            returncode: int = write_rc
        elif changed:
            has_error = False
            returncode = 1
        else:
            has_error = False
            returncode = 0

        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=write_commandline,
            returncode=returncode,
            has_error=has_error,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )

    # 通常モード: Step1 (check) → Step2 (write)
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    check_proc = _run_subprocess(check_commandline, env, on_output)
    check_rc = check_proc.returncode

    if check_rc == 0:
        # 整形不要
        output = check_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=check_commandline,
            returncode=0,
            has_error=False,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )

    # Step2: 書き込み
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    write_proc = _run_subprocess(write_commandline, env, on_output)
    output = write_proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    if write_proc.returncode != 0:
        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=write_commandline,
            returncode=write_proc.returncode,
            has_error=True,
            files=len(targets),
            output=output,
            elapsed=elapsed,
        )

    return CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=write_commandline,
        returncode=1,
        has_error=False,
        files=len(targets),
        output=check_proc.stdout.strip(),
        elapsed=elapsed,
    )


def _execute_prettier_two_step(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline_prefix: list[str],
    config: pyfltr.config.Config,
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_mode: bool,
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
) -> CommandResult:
    """Prettier の 2 段階実行 (prettier --check → prettier --write)。

    `prettier --check` (read-only) と `prettier --write` (書き込み) は排他のため、
    既存の autoflake/isort/black の「同じ引数に --check を付与する」ダンスは使えない。
    本ヘルパーでは以下のとおり実行する。

    通常モード (fix_mode=False):

    - Step1: `prefix + args + check-args + additional + targets` を実行
    - Step1 rc == 0 → succeeded (書き込み不要)
    - Step1 rc == 1 → Step2 `prefix + args + write-args + additional + targets` を実行
      - Step2 rc == 0 → formatted (書き込み成功)
      - Step2 rc != 0 → failed
    - Step1 rc >= 2 → failed (設定ミス等)

    fix モード (fix_mode=True):

    - Step1 はスキップし、直接 `prefix + args + write-args + additional + targets` を実行
    - 書き込み検知には内容ハッシュスナップショットを使う
    - rc != 0 → failed
    - rc == 0 かつハッシュ変化あり → formatted
    - rc == 0 かつ変化なし → succeeded
    """
    common_args: list[str] = list(config[f"{command}-args"])
    check_args: list[str] = list(config[f"{command}-check-args"])
    write_args: list[str] = list(config[f"{command}-write-args"])
    target_strs = [str(t) for t in targets]

    write_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *write_args,
        *additional_args,
        *target_strs,
    ]

    if fix_mode:
        digests_before = _snapshot_file_digests(targets)
        if args.verbose and on_output is not None:
            on_output(f"commandline: {shlex.join(write_commandline)}\n")
        write_proc = _run_subprocess(write_commandline, env, on_output)
        write_rc = write_proc.returncode
        output = write_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        changed = _snapshot_file_digests(targets) != digests_before

        if write_rc != 0:
            has_error = True
            returncode: int = write_rc
            result_command_type: str = command_info.type
        elif changed:
            has_error = False
            returncode = 1
            result_command_type = "formatter"
        else:
            has_error = False
            returncode = 0
            result_command_type = command_info.type

        errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
        return CommandResult(
            command=command,
            command_type=result_command_type,
            commandline=write_commandline,
            returncode=returncode,
            has_error=has_error,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )

    # 通常モード: Step1 (check) → 必要なら Step2 (write)
    check_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *check_args,
        *additional_args,
        *target_strs,
    ]

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(check_commandline)}\n")
    step1_proc = _run_subprocess(check_commandline, env, on_output)
    step1_rc = step1_proc.returncode

    if step1_rc == 0:
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=check_commandline,
            returncode=0,
            has_error=False,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )

    if step1_rc >= 2:
        # 設定ミス等の致命的エラー。Step2 は実行しない。
        output = step1_proc.stdout.strip()
        elapsed = time.perf_counter() - start_time
        errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=check_commandline,
            returncode=step1_rc,
            has_error=True,
            files=len(targets),
            output=output,
            elapsed=elapsed,
            errors=errors,
        )

    # Step1 rc == 1 → Step2 実行 (書き込み)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(write_commandline)}\n")
    step2_proc = _run_subprocess(write_commandline, env, on_output)
    step2_rc = step2_proc.returncode
    output = (step1_proc.stdout + step2_proc.stdout).strip()
    elapsed = time.perf_counter() - start_time

    if step2_rc == 0:
        has_error = False
        returncode = 1  # formatted 扱い
    else:
        has_error = True
        returncode = step2_rc

    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)
    return CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=write_commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
        errors=errors,
    )


def _snapshot_file_digests(targets: list[pathlib.Path]) -> dict[pathlib.Path, bytes]:
    """対象ファイルの内容ハッシュ (BLAKE2b) スナップショットを取得。

    mtime ベースの比較は textlint --fix のように「残存違反がなくても
    ファイルを書き戻す」ツールで偽陽性を起こすため、内容ハッシュで比較する。
    ファイルが存在しない場合は空 bytes を設定する (比較で差分検知できる)。
    """
    result: dict[pathlib.Path, bytes] = {}
    for target in targets:
        try:
            with target.open("rb") as f:
                result[target] = hashlib.file_digest(f, "blake2b").digest()
        except OSError:
            result[target] = b""
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

    # .gitignore フィルタリング
    if config["respect-gitignore"]:
        expanded = _filter_by_gitignore(expanded)

    return expanded


def _filter_by_gitignore(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    """Git check-ignore で .gitignore に該当するファイルを除外する。"""
    if not paths:
        return paths
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(str(p) for p in paths),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        logger.warning("git が見つからないため respect-gitignore をスキップする")
        return paths
    except subprocess.TimeoutExpired:
        logger.warning("git check-ignore がタイムアウトしたためスキップする")
        return paths
    if result.returncode not in (0, 1):
        # 0: 1つ以上 ignored, 1: 全て not ignored, 128: fatal error（リポジトリ外等）
        logger.debug("git check-ignore が終了コード %d を返した", result.returncode)
        return paths
    ignored_set: set[str] = set()
    if result.stdout:
        ignored_set = {s for s in result.stdout.split("\0") if s}
    return [p for p in paths if str(p) not in ignored_set]


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
