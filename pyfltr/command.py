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
import signal
import subprocess
import threading
import time
import typing

import natsort
import psutil

import pyfltr.config
import pyfltr.error_parser
import pyfltr.paths
import pyfltr.precommit
import pyfltr.warnings_

if typing.TYPE_CHECKING:
    import pyfltr.cache
    import pyfltr.only_failed

logger = logging.getLogger(__name__)

_active_processes: list[subprocess.Popen] = []  # type: ignore[type-arg]
# ``_active_processes`` への多スレッドアクセスを直列化する。
# --fail-fast が別スレッドから Popen.terminate() を呼ぶ際に、実行中プロセスの
# 追加・削除と衝突させないための明示ロック。
_active_processes_lock = threading.Lock()


class InterruptedExecution(Exception):
    """TUI から協調停止が要求されたことを示す例外。

    ``_run_subprocess`` が ``is_interrupted`` コールバックで中断指示を検知した際に送出する。
    呼び出し側（``ui._execute_command``）で捕捉し、当該コマンドを ``skipped`` 結果として置き換える。
    """


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
    "ec": BinToolSpec(bin_name="ec", mise_backend="editorconfig-checker"),
    "shellcheck": BinToolSpec(bin_name="shellcheck"),
    "shfmt": BinToolSpec(bin_name="shfmt"),
    "typos": BinToolSpec(bin_name="typos"),
    "actionlint": BinToolSpec(bin_name="actionlint"),
}


@dataclasses.dataclass(frozen=True)
class _StructuredOutputSpec:
    """構造化出力用の引数注入仕様。

    `-args` とは独立した経路で出力形式引数を強制注入する。
    注入時は commandline から conflicts に一致する既存引数を除去したうえで
    inject を追加する（ruff/typos は重複指定でエラーになるため）。
    """

    inject: list[str]
    """注入する引数"""
    conflicts: list[str]
    """commandline から除去する引数プレフィクス"""
    lint_only: bool = False
    """True のとき fix モードでは注入しない"""


# 各ツールの構造化出力用引数。設定キー → 注入仕様のマッピング。
# 設定キー（例: "ruff-check-json"）が True のとき有効になる。
_STRUCTURED_OUTPUT_SPECS: dict[str, tuple[str, _StructuredOutputSpec]] = {
    "ruff-check-json": (
        "ruff-check",
        _StructuredOutputSpec(
            inject=["--output-format=json"],
            conflicts=["--output-format"],
        ),
    ),
    "pylint-json": (
        "pylint",
        _StructuredOutputSpec(
            inject=["--output-format=json2"],
            conflicts=["--output-format"],
        ),
    ),
    "pyright-json": (
        "pyright",
        _StructuredOutputSpec(
            inject=["--outputjson"],
            conflicts=["--outputjson"],
        ),
    ),
    "pytest-tb-line": (
        "pytest",
        _StructuredOutputSpec(
            inject=["--tb=short"],
            conflicts=["--tb"],
        ),
    ),
    "shellcheck-json": (
        "shellcheck",
        _StructuredOutputSpec(
            inject=["-f", "json"],
            conflicts=["-f"],
        ),
    ),
    "textlint-json": (
        "textlint",
        _StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
            lint_only=True,
        ),
    ),
    "typos-json": (
        "typos",
        _StructuredOutputSpec(
            inject=["--format=json"],
            conflicts=["--format"],
        ),
    ),
    "eslint-json": (
        "eslint",
        _StructuredOutputSpec(
            inject=["--format", "json"],
            conflicts=["--format"],
        ),
    ),
    "biome-json": (
        "biome",
        _StructuredOutputSpec(
            inject=["--reporter=github"],
            conflicts=["--reporter"],
        ),
    ),
}


def _get_structured_output_spec(command: str, config: pyfltr.config.Config) -> _StructuredOutputSpec | None:
    """コマンドに対応する構造化出力仕様を返す。無効化されていれば None。"""
    for config_key, entry in _STRUCTURED_OUTPUT_SPECS.items():
        cmd = entry[0]
        spec = entry[1]
        if cmd == command and config.values.get(config_key, False):
            return spec
    return None


def _apply_structured_output(commandline: list[str], spec: _StructuredOutputSpec) -> list[str]:
    """Commandline から衝突する引数を除去し、構造化出力引数を注入する。"""
    filtered: list[str] = []
    skip_next = False
    for i, arg in enumerate(commandline):
        if skip_next:
            skip_next = False
            continue
        matched = False
        for prefix in spec.conflicts:
            if arg == prefix:
                # "-f gcc" 形式: 次の引数もスキップ
                if i + 1 < len(commandline) and not commandline[i + 1].startswith("-"):
                    skip_next = True
                matched = True
                break
            if arg.startswith(f"{prefix}=") or (arg.startswith(prefix) and arg != prefix):
                # "--format=json" 形式 / "--outputjson" 形式
                matched = True
                break
        if not matched:
            filtered.append(arg)
    return [*filtered, *spec.inject]


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


def _failed_resolution_result(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    message: str,
) -> "CommandResult":
    """ツール解決失敗時の `CommandResult` を組み立てる。"""
    pyfltr.warnings_.emit_warning(source="tool-resolve", message=f"{command}: {message}")
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


def _kill_process_tree(proc: "subprocess.Popen[str]", *, timeout: float) -> None:
    """Proc とその子孫をまとめて停止する。

    ``_run_subprocess`` は POSIX では ``start_new_session=True``、Windows では
    ``CREATE_NEW_PROCESS_GROUP`` で Popen を起動している。pytest-xdist のように
    サブプロセスが更にサブプロセスを fork してパイプを継承するツールでは、
    親だけ ``terminate()`` しても孫が stdout を握り続け ``for line in proc.stdout``
    が EOF を受け取れない。これを回避するため、親子孫を一括で停止する。

    POSIX: ``os.killpg(pgid, SIGTERM)`` → ``timeout`` 秒待機 → 残存に
    ``os.killpg(pgid, SIGKILL)``。``start_new_session=True`` により pgid は proc.pid と
    一致するので、親が既に reap されていても pid=pgid として停止シグナルを届けられる。

    Windows: 完全な Job Object を導入しない簡易実装。親消失後に ``children(recursive=True)``
    では子孫を辿れないため、先に列挙して ``terminate()`` を送り、その後 ``wait_procs`` で
    残存に ``kill()`` を送る。サブプロセスが更に分離 Job Object を使う場合は取り逃すが、
    現状の pyfltr 対応ツールでは問題にならない範囲とする。
    """
    targets: list[psutil.Process] = []
    if os.name == "nt":
        # 親消失後に辿れなくなるため、事前に子孫 pid 集合を取得する。
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            parent = psutil.Process(proc.pid)
            targets = parent.children(recursive=True)
        with contextlib.suppress(OSError):
            proc.terminate()
        for child in targets:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                child.terminate()
    else:
        # os.killpg / os.getpgid / signal.SIGKILL は POSIX 専用で Windows 型スタブに未定義。
        # os.name ガード下なので実行時は安全。型チェッカーの誤検知だけ局所コメントで抑止する。
        try:
            pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore
        except ProcessLookupError:
            # 親プロセスが既に reap されている。start_new_session=True により
            # pgid == pid として設定されていたはずなので pid をそのまま使う。
            pgid = proc.pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore

    # psutil.Process は失敗時も自身を含めて扱うため None チェックのうえで wait 対象に含める。
    wait_targets: list[psutil.Process] = list(targets)
    with contextlib.suppress(psutil.NoSuchProcess):
        wait_targets.append(psutil.Process(proc.pid))

    _, alive = psutil.wait_procs(wait_targets, timeout=timeout)

    # 残存プロセスへ SIGKILL / kill を送る。
    if alive:
        if os.name == "nt":
            for child in alive:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    child.kill()
        else:
            try:
                pgid = os.getpgid(proc.pid)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore
            except ProcessLookupError:
                pgid = proc.pid
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore
        _, still_alive = psutil.wait_procs(alive, timeout=timeout)
        if still_alive:
            remaining_pids = [p.pid for p in still_alive]
            logger.warning("プロセスツリー停止後に残存するプロセスあり: pids=%s", remaining_pids)


def _cleanup_processes() -> None:
    """プロセス終了時に実行中の子プロセスを終了。"""
    with _active_processes_lock:
        procs = list(_active_processes)
    for proc in procs:
        with contextlib.suppress(OSError):
            _kill_process_tree(proc, timeout=1.0)


atexit.register(_cleanup_processes)


def terminate_active_processes(*, timeout: float = 5.0) -> None:
    """実行中のすべての子プロセスと子孫に terminate() → kill() を送る。

    --fail-fast や TUI Ctrl+C 協調停止で、並列実行中の他ツールを止めるために呼ばれる。
    ``_kill_process_tree`` 経由でプロセスグループ単位 (POSIX) / 子孫 pid 列挙 (Windows)
    で停止するため、pytest-xdist のように Popen 子が更にサブプロセスを fork する
    ツールでも確実に停止する。
    """
    with _active_processes_lock:
        procs = list(_active_processes)
    for proc in procs:
        with contextlib.suppress(OSError):
            _kill_process_tree(proc, timeout=timeout)


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
    target_files: list[pathlib.Path] = dataclasses.field(default_factory=list)
    """当該ツールに渡したターゲットファイル一覧 (retry_command の位置引数復元に使用)。

    ``pass-filenames=False`` のツールでは ``commandline`` にファイルが含まれないため、
    retry_command でターゲットを差し替えるには実行時点のリストを別途保持する必要がある。
    """
    archived: bool = False
    """実行アーカイブへの書き込みに成功したか。

    ``True`` のときに限り、JSONL 側で smart truncation によるメッセージ/diagnostic 省略を
    適用できる (切り詰め分はアーカイブから復元可能)。``--no-archive`` やアーカイブ初期化
    失敗時は ``False`` のままとなり、切り詰めをスキップして全文を JSONL に出力する。
    """
    retry_command: str | None = None
    """当該ツール 1 件を再実行するための shell コマンド文字列 (tool レコード用)。

    ``run_pipeline`` がツール完了時に埋める。未設定 (``None``) のときは tool レコードから
    省略する (テスト等、パイプライン外で CommandResult を生成する場合)。
    """
    cached: bool = False
    """ファイル hash キャッシュから復元された結果か否か。

    ``True`` のとき、当該ツールは実際には実行されておらず、過去の実行結果を復元して
    返されている。``--no-cache`` またはキャッシュ未ヒットの場合は ``False``。
    """
    cached_from: str | None = None
    """キャッシュヒット時の復元元 run_id (ULID)。

    ``cached=True`` のときに限り設定される。JSONL tool レコードで参照誘導用に出力する
    (``show-run`` / MCP の詳細参照経路で当該 run の全文を確認できる)。
    """

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


def _get_env_path(env: dict[str, str]) -> str | None:
    """``env`` から PATH 値を取り出す。

    Windows は環境変数名が大文字小文字非区別のため ``env`` キーを非依存探索する
    (``env={"Path": "..."}`` のように大小が混在していても拾う)。POSIX で同じ探索を
    行うと ``env={"Path": "/tmp/bin", "PATH": "/usr/bin"}`` のようなケースで解決側と
    Popen 実行時側の PATH が不一致となるため、POSIX では ``env.get("PATH")`` のみを
    使う。
    """
    if os.name == "nt":
        for key, value in env.items():
            if key.upper() == "PATH":
                return value
        return None
    return env.get("PATH")


def _terminate_and_drop(proc: "subprocess.Popen[str]") -> None:
    """実行中 proc とその子孫を停止し ``_active_processes`` から外す。

    TUI 協調停止経路で使う。``with subprocess.Popen(...)`` の __exit__ は子が残っていても
    ``wait()`` で止まってしまうため、``InterruptedExecution`` を送出する前に本関数で
    確実に子を終了させる。pytest-xdist など孫プロセスを fork するツールを想定し、
    ``_kill_process_tree`` でプロセスツリー単位で停止する。
    """
    with contextlib.suppress(OSError):
        _kill_process_tree(proc, timeout=5.0)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=5.0)
    with _active_processes_lock, contextlib.suppress(ValueError):
        _active_processes.remove(proc)


def _run_subprocess(
    commandline: list[str],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None = None,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """サブプロセスの実行 (Popen ベース)。

    --fail-fast で並列実行中の他プロセスを外部スレッドから terminate() できるよう、
    subprocess.run の経路も Popen に統一し ``_active_processes`` に登録する。
    ``on_output`` が指定されている場合は逐次コールバックを呼び、未指定時は最後に
    全出力をまとめて返す。

    ``is_interrupted`` が指定された場合、(1) ``Popen`` 呼び出し直前、(2) ``Popen`` 生成直後、
    (3) stdout 読み出しループの各イテレーション冒頭の 3 点で中断指示を確認し、真なら
    当該 proc を確実に終了させてから ``InterruptedExecution`` を送出する。TUI 協調停止経路で
    使う。``on_subprocess_start`` / ``on_subprocess_end`` は subprocess が実際に動いている
    区間を追跡するためのフック（UI 側で「実行中コマンド集合」を正確に保つのに使う）。
    start 後は必ず finally で end を呼ぶため、Ctrl+C スナップショットにフック外の時間帯が
    混入しない。

    Windows では ``subprocess.Popen`` を ``shell=False`` でリスト渡しにすると
    ``.exe`` / ``.cmd`` 等の拡張子付きファイルを PATH から自動解決しないため、
    ここで ``shutil.which`` を使って ``commandline[0]`` をフルパスへ解決する。
    引数の ``commandline`` は書き換えず、Popen に渡す一時リストのみで差し替える
    (CommandResult.commandline や retry_command に解決後のフルパスが混入して
    ポータビリティが損なわれるのを避けるため)。解決探索対象 PATH は Popen に
    渡す ``env`` の PATH 値と一致させる (隔離した env で見えない実行ファイルを
    起動したり、逆に env でだけ見える実行ファイルを解決できない事故を避ける)。
    Windows では環境変数名が大文字小文字非区別のため env キーを非依存探索する。
    解決できなかった場合は元のコマンド名のまま Popen に渡し、既存の
    FileNotFoundError 経路で rc=127 の `CompletedProcess` に変換する。
    """
    popen_commandline = commandline
    env_path = _get_env_path(env)
    resolved = shutil.which(commandline[0], path=env_path)
    if resolved is not None and resolved != commandline[0]:
        popen_commandline = [resolved, *commandline[1:]]
    # (1) Popen 直前の中断チェック。proc がまだ存在しないのでそのまま送出できる。
    if is_interrupted is not None and is_interrupted():
        raise InterruptedExecution
    # OS 別のプロセスグループ分離オプション。pytest-xdist など孫プロセスを
    # fork するツールの中断時に、親子孫をまとめて停止できるようにする。
    popen_extra: dict[str, typing.Any] = {}
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP は Windows 専用の定数。getattr の 3 引数形式を使うと
        # ruff B009 の getattr→属性アクセス変換対象外になるため、型チェッカー誤検知を回避できる。
        popen_extra["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_extra["start_new_session"] = True
    try:
        with subprocess.Popen(
            popen_commandline,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            **popen_extra,
        ) as proc:
            with _active_processes_lock:
                _active_processes.append(proc)
            subprocess_started = False
            try:
                if on_subprocess_start is not None:
                    on_subprocess_start()
                subprocess_started = True
                # (2) Popen 生成直後の中断チェック。_active_processes 登録済みなので
                # _terminate_and_drop で自己登録を外してから送出する。
                if is_interrupted is not None and is_interrupted():
                    _terminate_and_drop(proc)
                    raise InterruptedExecution

                output_lines: list[str] = []
                assert proc.stdout is not None
                for line in proc.stdout:
                    # (3) 各イテレーション冒頭の中断チェック。
                    if is_interrupted is not None and is_interrupted():
                        _terminate_and_drop(proc)
                        raise InterruptedExecution
                    output_lines.append(line)
                    if on_output is not None:
                        on_output(line)
                proc.wait()
                return subprocess.CompletedProcess(
                    args=commandline,
                    returncode=proc.returncode,
                    stdout="".join(output_lines),
                )
            finally:
                if subprocess_started and on_subprocess_end is not None:
                    on_subprocess_end()
                with _active_processes_lock, contextlib.suppress(ValueError):
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


def pick_targets(
    only_failed_targets: "dict[str, pyfltr.only_failed.ToolTargets] | None",
    command: str,
) -> "pyfltr.only_failed.ToolTargets | None":
    """``only_failed_targets`` から当該ツールの ToolTargets を取り出す。

    ``only_failed_targets`` 自体が ``None`` の場合（``--only-failed`` 未指定）は常に
    ``None`` を返し、``execute_command`` で既定の ``all_files`` に委ねる。指定あり時は
    dict から当該コマンドのエントリを返す（存在しない場合は None）。
    ``cli`` と ``ui`` の両経路から同一挙動で引ける共通ヘルパー。
    """
    if only_failed_targets is None:
        return None
    return only_failed_targets.get(command)


def execute_command(
    command: str,
    args: argparse.Namespace,
    config: pyfltr.config.Config,
    all_files: list[pathlib.Path],
    on_output: typing.Callable[[str], None] | None = None,
    *,
    fix_stage: bool = False,
    cache_store: "pyfltr.cache.CacheStore | None" = None,
    cache_run_id: str | None = None,
    only_failed_targets: "pyfltr.only_failed.ToolTargets | None" = None,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """コマンドの実行。

    ``fix_stage=True`` の場合、当該コマンドが fix-args を持っていれば fix 経路
    （``--fix`` 付きの単発実行）で動作する。fix-args 未定義の formatter では
    通常経路と挙動が変わらないため、呼び出し側は fix ステージで走らせる対象を
    ``split_commands_for_execution()`` で絞り込んだうえで指定する前提。

    ``cache_store`` が指定され、かつ当該コマンドが ``CommandInfo.cacheable=True`` の
    非 fix モード実行なら、ファイル hash キャッシュを参照して一致があれば実行を
    スキップし、過去の結果を復元して ``cached=True`` で返す。キャッシュミス時は
    通常実行のうえ、成功 (rc=0, has_error=False) に限り ``cache_run_id`` をソースとして
    書き込む。``cache_run_id`` が ``None`` の場合はキャッシュ書き込みをスキップする
    (アーカイブ無効時に ``cached_from`` で参照させる元 run が無いため)。

    ``only_failed_targets`` が指定された場合、``ToolTargets.resolve_files(all_files)``
    経由で実対象ファイルを取得する（``--only-failed`` 経路でツール別の失敗ファイル集合を
    渡す用途）。その後の ``target_extensions`` / ``pass_filenames=False`` の分岐は
    通常通り適用される。``None`` の場合は既定の ``all_files`` を使用する。
    """
    command_info = config.commands[command]
    globs = command_info.target_globs()
    source_files = only_failed_targets.resolve_files(all_files) if only_failed_targets is not None else all_files
    targets: list[pathlib.Path] = filter_by_globs(source_files, globs)

    # ツール別excludeの適用（--no-excludeが指定された場合はスキップ）
    if not args.no_exclude:
        tool_excludes: list[str] = config.values.get(f"{command}-exclude", [])
        if tool_excludes:
            targets = [t for t in targets if not _matches_exclude_patterns(t, tool_excludes)]

    # ファイルの順番をシャッフルまたはソート（fix ステージは再現性重視でシャッフルを無効化）
    if args.shuffle and not fix_stage:
        random.shuffle(targets)
    else:
        # natsort.natsorted の型ヒントが不十分で ty が union 型へ縮めるため cast で明示。
        targets = typing.cast("list[pathlib.Path]", natsort.natsorted(targets, key=str))

    # fix ステージでは当該コマンドの fix-args を引用して fix 経路に分岐する。
    # fix-args 未定義の formatter は通常経路を通る（通常実行でもファイルを書き換えるため挙動は同じ）。
    fix_mode = fix_stage
    fix_args: list[str] | None = None
    if fix_mode:
        fix_args = config.values.get(f"{command}-fix-args")

    # textlint / markdownlint は path が空の場合、js-runner 設定から解決する。
    # ec 等は bin-runner 設定から解決する。
    if command in _JS_TOOL_BIN and config[f"{command}-path"] == "":
        try:
            resolved_path, prefix = _resolve_js_commandline(command, config)
        except FileNotFoundError as e:
            return _failed_resolution_result(
                command,
                command_info,
                f"js-runner=direct 指定ですが実行ファイルが見つかりません: {e}. "
                "package.jsonで対象パッケージをインストールしてください。",
            )
        commandline_prefix: list[str] = [resolved_path, *prefix]
    elif command in _BIN_TOOL_SPEC and config[f"{command}-path"] == "":
        try:
            resolved_path, prefix = _resolve_bin_commandline(command, config)
        except FileNotFoundError as e:
            return _failed_resolution_result(command, command_info, f"ツールが見つかりません: {e}")
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
    # 構造化出力引数の注入（-args とは独立した経路で出力形式を強制する）
    structured_spec = _get_structured_output_spec(command, config)
    if structured_spec is not None and not (structured_spec.lint_only and fix_args is not None):
        commandline = _apply_structured_output(commandline, structured_spec)
    # pass-filenames = false のツールはファイル引数を渡さない（tsc 等）
    if config.values.get(f"{command}-pass-filenames", True):
        commandline.extend(str(t) for t in targets)

    # 各 CommandResult に当該ツールのターゲットファイル一覧を埋めるためのヘルパー。
    # retry_command で差し替え可能なターゲットを復元するのに使う (特に pass-filenames=False
    # のツールでは commandline からも復元できないため、ここで明示的に保持する)。
    def _with_targets(result: CommandResult) -> CommandResult:
        result.target_files = list(targets)
        return result

    if len(targets) <= 0:
        return _with_targets(
            CommandResult(
                command=command,
                command_type=command_info.type,
                commandline=commandline,
                returncode=None,
                has_error=False,
                output="対象ファイルが見つかりません。",
                files=0,
                elapsed=0,
            )
        )

    start_time = time.perf_counter()
    env = _build_subprocess_env(config, command)

    # pre-commit は .pre-commit-config.yaml を参照して SKIP 環境変数を構築し、
    # pyfltr 関連 hook を除外したうえで 2 段階実行する。
    # stage 1 でファイル修正のみ (fixer 系) なら "formatted"、
    # checker 系 hook が残存エラーを報告すれば "failed" となる。
    if command == "pre-commit":
        return _with_targets(
            _execute_pre_commit(
                command,
                command_info,
                commandline,
                targets,
                config,
                args,
                env,
                on_output,
                start_time,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # textlint の fix モードは 2 段階実行 (fix 適用 + lint チェック)。
    # fixer-formatter が compact をサポートしない問題と、残存違反を compact で取得する
    # 要件を両立させるため、他の linter とは別経路で実行する。
    if fix_args is not None and command == "textlint":
        return _with_targets(
            _execute_textlint_fix(
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
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # fix モードで linter に fix-args を適用する経路。
    # mtime 変化で formatted 判定を行い、rc != 0 はそのまま failed 扱いとする。
    if fix_args is not None and command_info.type != "formatter":
        return _with_targets(
            _execute_linter_fix(
                command,
                command_info,
                commandline,
                targets,
                env,
                on_output,
                start_time,
                args,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # ruff-formatで ruff-format-by-check が有効な場合は、
    # 先に ruff check --fix --unsafe-fixes を実行してから ruff format を実行する。
    # ステップ1(check)の lint violation (exit 1) は無視する (lint は ruff-check で検出)。
    # ただし exit >= 2 (設定エラー等) は失敗扱いする。
    if command == "ruff-format" and config["ruff-format-by-check"]:
        return _with_targets(
            _execute_ruff_format_two_step(
                command,
                command_info,
                commandline,
                targets,
                config,
                args,
                env,
                on_output,
                start_time,
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # shfmt は -l (確認) と -w (書き込み) が排他のため prettier 同様の 2 段階実行。
    if command == "shfmt":
        return _with_targets(
            _execute_shfmt_two_step(
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
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    # prettier は --check (read-only) と --write (書き込み) が排他のため 2 段階実行する。
    # ruff-format と同じ位置・スタイルで分岐する。
    # prettier には {cmd}-fix-args を定義していないため fix 判定は fix_stage 由来の
    # fix_mode 変数を使う (filter_fix_commands では formatter として常に fix 対象となる)。
    if command == "prettier":
        return _with_targets(
            _execute_prettier_two_step(
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
                is_interrupted=is_interrupted,
                on_subprocess_start=on_subprocess_start,
                on_subprocess_end=on_subprocess_end,
            )
        )

    has_error = False

    # ファイル hash キャッシュの参照 (cacheable=True の非 fix 実行のみ)。
    # 実装簡潔化のため、cacheable=True のコマンドは本 plain 経路でのみキャッシュを
    # 扱う (textlint の fix モードは _execute_textlint_fix 経由なので対象外)。
    # キャッシュ対象判定 / キー算出 / 書き込みを break/resume できるよう、結果を
    # 後段で差し替える設計とする。
    cache_context = _prepare_cache_context(
        command,
        command_info,
        config,
        commandline,
        targets,
        additional_args,
        fix_args=fix_args,
        cache_store=cache_store,
    )
    if cache_context is not None:
        cached_result = cache_context.lookup()
        if cached_result is not None:
            cached_result.target_files = list(targets)
            # 復元値の files / elapsed は過去実行時のもの。復元時の実ファイル数は
            # 現在のターゲットリストに合わせ直す (再実行時の対象件数表示のため)。
            cached_result.files = len(targets)
            return cached_result

    # verbose時はコマンドラインをon_output経由で出力
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
    proc = _run_subprocess(
        commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    returncode = proc.returncode

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    # エラー箇所のパース
    errors = pyfltr.error_parser.parse_errors(command, output, command_info.error_pattern)

    result = _with_targets(
        CommandResult(
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
    )

    # キャッシュ書き込み (成功 rc=0 のみ)。失敗結果を記録すると再試行で同じ失敗が
    # 復元されて修正確認できなくなるため、成功時に限定する。
    if cache_context is not None and returncode == 0 and not has_error:
        cache_context.store(result, run_id=cache_run_id)

    return result


@dataclasses.dataclass
class _CacheContext:
    """キャッシュ参照用のコンテキスト。

    ``execute_command`` の plain 経路でのみ使う内部ヘルパー。
    """

    cache_store: "pyfltr.cache.CacheStore"
    command: str
    key: str

    def lookup(self) -> CommandResult | None:
        """キャッシュを参照する。ヒットなら CommandResult、ミスなら None。"""
        return self.cache_store.get(self.command, self.key)

    def store(self, result: CommandResult, *, run_id: str | None) -> None:
        """キャッシュへ書き込む (ソース run_id 付き)。"""
        self.cache_store.put(self.command, self.key, result, run_id=run_id)


def _prepare_cache_context(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    config: pyfltr.config.Config,
    commandline: list[str],
    targets: list[pathlib.Path],
    additional_args: list[str],
    *,
    fix_args: list[str] | None,
    cache_store: "pyfltr.cache.CacheStore | None",
) -> _CacheContext | None:
    """キャッシュ参照用のキー算出。対象外の場合は None を返す。"""
    if cache_store is None or not command_info.cacheable or fix_args is not None:
        return None
    import pyfltr.cache  # pylint: disable=import-outside-toplevel

    if not pyfltr.cache.is_cacheable(command, config, additional_args):
        return None
    structured_spec = _get_structured_output_spec(command, config)
    key = cache_store.compute_key(
        command=command,
        commandline=commandline,
        fix_stage=False,
        structured_output=structured_spec is not None,
        target_files=targets,
        config_files=pyfltr.cache.resolve_config_files(command, config),
    )
    return _CacheContext(cache_store=cache_store, command=command, key=key)


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
    # Windows の cp932/cp1252 などに依存せず、ツール側の open()/Path.read_text() を UTF-8 で動かす。
    # 例: uv-sort が pyproject.toml をエンコーディング未指定で読み込む箇所で発生する
    # UnicodeDecodeError を回避する。
    env["PYTHONUTF8"] = "1"
    if config.values.get(f"{command}-devmode", False):
        env["PYTHONDEVMODE"] = "1"
    # 表示幅を適切な範囲に制限する
    # (pytestなどは一部の表示が右寄せになるのであまり大きいと見づらい)
    env["COLUMNS"] = str(min(max(shutil.get_terminal_size().columns - 4, 80), 128))
    return env


def _execute_pre_commit(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    config: pyfltr.config.Config,
    args: argparse.Namespace,
    env: dict[str, str] | None,
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
) -> CommandResult:
    """pre-commit の 2 段階実行。

    stage 1 で pre-commit run --all-files を実行し、fixer 系 hook がファイルを
    修正しただけなら再実行で成功する（"formatted"）。checker 系 hook のエラーが
    残る場合は "failed"（has_error=True）として返す。
    """
    # pre-commit 配下から起動された場合は自身を再帰実行しない。
    # git commit → pre-commit → pyfltr fast → pre-commit の二重実行を防ぐ。
    if pyfltr.precommit.is_running_under_precommit():
        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=commandline,
            returncode=None,
            has_error=False,
            output="pre-commit 配下で実行されたため pre-commit 統合をスキップしました。",
            files=len(targets),
            elapsed=time.perf_counter() - start_time,
        )

    # .pre-commit-config.yaml が存在しなければスキップ
    config_dir = pathlib.Path.cwd()
    config_path = config_dir / ".pre-commit-config.yaml"
    if not config_path.exists():
        return CommandResult(
            command=command,
            command_type=command_info.type,
            commandline=commandline,
            returncode=None,
            has_error=False,
            output=".pre-commit-config.yaml が見つかりません。",
            files=len(targets),
            elapsed=time.perf_counter() - start_time,
        )

    # SKIP 環境変数を構築（pyfltr 関連 hook を除外して再帰を防止）
    skip_value = pyfltr.precommit.build_skip_value(config, config_dir)
    pre_commit_env = dict(env) if env is not None else dict(os.environ)
    if skip_value:
        existing_skip = pre_commit_env.get("SKIP", "")
        if existing_skip:
            pre_commit_env["SKIP"] = f"{existing_skip},{skip_value}"
        else:
            pre_commit_env["SKIP"] = skip_value

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(commandline)}\n")
        if skip_value:
            on_output(f"SKIP={pre_commit_env.get('SKIP', '')}\n")

    # stage 1: 実行
    proc = _run_subprocess(
        commandline,
        pre_commit_env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    returncode = proc.returncode
    has_error = False

    # stage 2: 失敗時は再実行（fixer が修正しただけなら 2 回目で成功する）
    if returncode != 0:
        if args.verbose and on_output is not None:
            on_output("pre-commit: stage 2 再実行\n")
        proc = _run_subprocess(
            commandline,
            pre_commit_env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
        if proc.returncode != 0:
            returncode = proc.returncode
            has_error = True

    output = proc.stdout.strip()
    elapsed = time.perf_counter() - start_time

    return CommandResult(
        command=command,
        command_type=command_info.type,
        commandline=commandline,
        returncode=returncode,
        has_error=has_error,
        files=len(targets),
        output=output,
        elapsed=elapsed,
    )


def _execute_linter_fix(
    command: str,
    command_info: pyfltr.config.CommandInfo,
    commandline: list[str],
    targets: list[pathlib.Path],
    env: dict[str, str],
    on_output: typing.Callable[[str], None] | None,
    start_time: float,
    args: argparse.Namespace,
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
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
    proc = _run_subprocess(
        commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
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
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
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
    # 保護対象識別子の事前検出 (Step1 で破損するケースを捕捉するため)。
    # 空リスト設定時は計測を省略する。
    protected_identifiers: list[str] = list(config.values.get("textlint-protected-identifiers", []))
    contents_before: dict[pathlib.Path, str] = _snapshot_file_texts(targets) if protected_identifiers else {}

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step1_commandline)}\n")
    step1_proc = _run_subprocess(
        step1_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode
    # rc=0 (違反なし) / rc=1 (違反残存) は通常終了、rc>=2 は致命的エラー扱い
    step1_fatal = step1_rc >= 2
    step1_changed = _snapshot_file_digests(targets) != digests_before

    if protected_identifiers and step1_changed:
        _warn_protected_identifier_corruption(contents_before, _snapshot_file_texts(targets), protected_identifiers)

    # Step2: 通常 lint 実行 (残存違反を取得)
    step2_commandline: list[str] = [
        *commandline_prefix,
        *common_args,
        *lint_args,
        *additional_args,
    ]
    # 構造化出力引数の注入（Step2 は lint フェーズなので lint_only でも適用する）
    structured_spec = _get_structured_output_spec(command, config)
    if structured_spec is not None:
        step2_commandline = _apply_structured_output(step2_commandline, structured_spec)
    step2_commandline.extend(target_strs)

    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(step2_commandline)}\n")
    step2_proc = _run_subprocess(
        step2_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
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
    *,
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
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
    step1_proc = _run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
    step1_rc = step1_proc.returncode
    step1_failed = step1_rc >= 2  # exit 0/1 は無視、2 以上 (abrupt termination) のみ失敗扱い
    step1_changed = _snapshot_file_digests(targets) != digests_before

    # ステップ2実行 (常に実行)
    if args.verbose and on_output is not None:
        on_output(f"commandline: {shlex.join(format_commandline)}\n")
    step2_proc = _run_subprocess(
        format_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
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
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
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
        write_proc = _run_subprocess(
            write_commandline,
            env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
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
    check_proc = _run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
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
    write_proc = _run_subprocess(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
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
    is_interrupted: typing.Callable[[], bool] | None = None,
    on_subprocess_start: typing.Callable[[], None] | None = None,
    on_subprocess_end: typing.Callable[[], None] | None = None,
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
        write_proc = _run_subprocess(
            write_commandline,
            env,
            on_output,
            is_interrupted=is_interrupted,
            on_subprocess_start=on_subprocess_start,
            on_subprocess_end=on_subprocess_end,
        )
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
    step1_proc = _run_subprocess(
        check_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
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
    step2_proc = _run_subprocess(
        write_commandline,
        env,
        on_output,
        is_interrupted=is_interrupted,
        on_subprocess_start=on_subprocess_start,
        on_subprocess_end=on_subprocess_end,
    )
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


def _snapshot_file_texts(targets: list[pathlib.Path]) -> dict[pathlib.Path, str]:
    """対象ファイルのテキスト内容スナップショットを取得する。

    textlint fix の保護対象識別子破損検知に使う。読み込めないファイルは辞書から
    除外する (比較時には「前後どちらにも出現しない」と解釈される)。
    """
    result: dict[pathlib.Path, str] = {}
    for target in targets:
        try:
            result[target] = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return result


def _warn_protected_identifier_corruption(
    before: dict[pathlib.Path, str],
    after: dict[pathlib.Path, str],
    protected_identifiers: list[str],
) -> None:
    """Textlint fix 後に保護対象識別子が失われていた場合、警告を発行する。

    fix 前のファイル内容に含まれていた識別子が fix 後に 1 件でも減っていれば、
    当該識別子が ``preset-jtf-style`` などの機械変換で破損した可能性が高い。
    検知は出現回数ベース (等号比較) で行い、単純な減少も破損として扱う。
    """
    for path, before_text in before.items():
        after_text = after.get(path)
        if after_text is None:
            continue
        if before_text == after_text:
            continue  # 変化なしの場合は検査不要
        for identifier in protected_identifiers:
            before_count = before_text.count(identifier)
            after_count = after_text.count(identifier)
            if before_count > after_count:
                pyfltr.warnings_.emit_warning(
                    source="textlint-identifier-corruption",
                    message=(
                        f"textlint fix が保護対象識別子を変換した可能性: "
                        f"{identifier!r} (file={pyfltr.paths.to_cwd_relative(path)}, "
                        f"before={before_count}, after={after_count})"
                    ),
                    hint="保護したい識別子はバックティックで囲むとtextlintのfixで改変されなくなる",
                )


def expand_all_files(targets: list[pathlib.Path], config: pyfltr.config.Config) -> list[pathlib.Path]:
    """対象ファイルの一括展開。

    ディレクトリ走査・excludeチェック・gitignoreフィルタリングを1回だけ実行し、
    全ファイルのリストを返す。コマンドごとのglobフィルタリングはfilter_by_globsで行う。
    """
    # 空ならカレントディレクトリを対象とする
    if len(targets) == 0:
        targets = [pathlib.Path(".")]

    # コマンドラインで直接指定されたファイル（ディレクトリでないもの）を記録
    directly_specified: set[pathlib.Path] = set()
    expanded: list[pathlib.Path] = []

    def _expand_target(target: pathlib.Path, *, is_direct: bool) -> None:
        try:
            if excluded(target, config):
                if is_direct:
                    pyfltr.warnings_.emit_warning(
                        source="file-resolver",
                        message=f"指定されたファイルが除外設定により無視されました: {target}",
                    )
                return
            if target.is_dir():
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
        is_direct = not target.is_dir()
        _expand_target(target, is_direct=is_direct)

    # .gitignore フィルタリング
    if config["respect-gitignore"]:
        before_gitignore = set(expanded)
        expanded = _filter_by_gitignore(expanded)
        # 直接指定されたファイルがgitignoreで除外された場合に警告
        for target in directly_specified:
            if target in before_gitignore and target not in set(expanded):
                pyfltr.warnings_.emit_warning(
                    source="file-resolver",
                    message=f"指定されたファイルが .gitignore により無視されました: {target}",
                )

    return expanded


def filter_by_globs(all_files: list[pathlib.Path], globs: list[str]) -> list[pathlib.Path]:
    """ファイルリストをglobパターンでフィルタリングする。"""
    return [f for f in all_files if any(f.match(glob) for glob in globs)]


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
        # 0: 1つ以上 ignored, 1: 全て not ignored, 128: fatal error（リポジトリ外等）
        logger.debug("git check-ignore が終了コード %d を返した", result.returncode)
        return paths
    ignored_set: set[str] = set()
    if result.stdout:
        ignored_set = {s for s in result.stdout.split("\0") if s}
    return [p for p in paths if str(p) not in ignored_set]


def _matches_exclude_patterns(path: pathlib.Path, patterns: list[str]) -> bool:
    """パスが除外パターンのいずれかに一致するか否かを返す。"""
    if any(path.match(glob) for glob in patterns):
        return True
    # 親ディレクトリに一致してもTrue
    part = path.parent
    for _ in range(len(path.parts) - 1):
        if any(part.match(glob) for glob in patterns):
            return True
        part = part.parent
    return False


def excluded(path: pathlib.Path, config: pyfltr.config.Config) -> bool:
    """無視パターンチェック。"""
    excludes = config["exclude"] + config["extend-exclude"]
    return _matches_exclude_patterns(path, excludes)
