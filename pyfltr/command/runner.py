import dataclasses
import functools
import os
import pathlib
import platform
import shutil
import sys

import pyfltr.command.markdownlint_env
import pyfltr.command.mise
import pyfltr.command.process
import pyfltr.command.structured_output
import pyfltr.command.subprojects
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tools
from pyfltr.command.env import build_mise_subprocess_env
from pyfltr.tool_types import BinToolSpec
from pyfltr.tools import COMMAND_RUNNERS, JS_RUNNERS

logger = __import__("logging").getLogger(__name__)

MISE_TRUST_FAILURE_PREFIX = "mise trust --yes --all: "
"""`ensure_mise_available`がtrust失敗時に送出する`FileNotFoundError`の引数の接頭辞。

catch側（`pyfltr.command.tool_resolution.format_tool_resolution_failure`）が原因種別の判定に使う。
"""


class RunnerMismatchError(ValueError):
    def __init__(self, command: str, runner: str, effective: str, *, category: str, alternatives: str) -> None:
        resolved_text = f'（`{runner}`の設定値 "{effective}" に解決）' if runner != effective else ""
        default = pyfltr.config.model.command_setting(pyfltr.config.model.DEFAULT_CONFIG, command, "runner", "direct")
        super().__init__(
            f'{command}は{category}のツールではないため、`{command}-runner = "{runner}"`{resolved_text}では起動できません'
        )
        self.hint = (
            f'`{command}-runner`を削除して既定値 "{default}" に戻すか、'
            f"{alternatives} などツールの系統に合う値を指定してください。"
        )


JS_TOOL_BIN = {command: info.js_bin for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.js_bin is not None}

PYTHON_TOOL_BIN = {
    command: info.python_bin for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.python_bin is not None
}

PACKAGE_MANAGER_TOOL_BIN = {
    command: info.package_manager_bin
    for command, info in pyfltr.tools.BUILTIN_COMMANDS.items()
    if info.package_manager_bin is not None
}

_JS_TOOL_PNPX_PACKAGE_SPEC = {
    command: info.pnpx_package for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.pnpx_package is not None
}


_BIN_TOOL_SPEC = {
    command: info.bin_spec for command, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.bin_spec is not None
}


@dataclasses.dataclass(frozen=True)
class ResolvedCommandline:
    executable: str
    prefix: list[str]
    runner: str
    runner_source: str
    effective_runner: str
    tool_spec_omitted: bool = False
    runner_fallback: str | None = None
    markdownlint_environment: pyfltr.command.markdownlint_env.MarkdownlintEnvironment | None = None

    @property
    def commandline(self) -> list[str]:
        return [self.executable, *self.prefix]


def resolve_runner(command: str, config: pyfltr.config.model.Config) -> tuple[str, str]:
    runner = pyfltr.config.model.command_setting(config.values, command, "runner", None)
    if runner is None:
        return "direct", "default"
    default_runner = pyfltr.config.model.command_setting(pyfltr.config.model.DEFAULT_CONFIG, command, "runner", None)
    source = "default" if runner == default_runner else "explicit"
    return str(runner), source


def get_mise_active_tool_key(command: str) -> str | None:
    spec = _BIN_TOOL_SPEC.get(command)
    if spec is None:
        return None
    return spec.mise_backend or spec.bin_name


def _is_tool_active_in_mise_config(
    command: str,
    spec: BinToolSpec,
    config: pyfltr.config.model.Config,
    *,
    allow_side_effects: bool,
    cwd: pathlib.Path | None = None,
) -> bool:
    del command
    result = pyfltr.command.mise.get_mise_active_tools(config, allow_side_effects=allow_side_effects, cwd=cwd)
    key = spec.mise_backend or spec.bin_name
    return key in result.tools


@functools.lru_cache(maxsize=1)
def ensure_uv_available() -> bool:
    return shutil.which("uv") is not None


@functools.lru_cache(maxsize=1)
def ensure_uvx_available() -> bool:
    return shutil.which("uvx") is not None


@functools.lru_cache(maxsize=1)
def cwd_has_uv_lock(
    cwd: pathlib.Path | None = None,
    workspace_root: pathlib.Path | None = None,
) -> bool:
    effective_cwd = cwd if cwd is not None else pathlib.Path.cwd()
    return pyfltr.command.subprojects.find_uv_lock_for_cwd(effective_cwd, workspace_root=workspace_root) is not None


def _resolve_python_tool_direct(command: str) -> str:
    bin_name = PYTHON_TOOL_BIN[command]
    resolved = shutil.which(bin_name)
    if resolved is None:
        raise FileNotFoundError(bin_name)
    return resolved


def _resolve_direct_executable(bin_name: str) -> str:
    if bin_name == "dotnet":
        dotnet_root = os.environ.get("DOTNET_ROOT")
        if dotnet_root:
            for candidate_name in ("dotnet.exe", "dotnet") if os.name == "nt" else ("dotnet",):
                candidate = pathlib.Path(dotnet_root) / candidate_name
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)
    resolved = shutil.which(bin_name)
    if resolved is None:
        raise FileNotFoundError(bin_name)
    return resolved


def _resolve_python_commandline(
    command: str,
    effective: str,
    *,
    cwd: pathlib.Path | None = None,
    uv_workspace_root: pathlib.Path | None = None,
) -> tuple[str, str, list[str], str | None]:
    bin_name = PYTHON_TOOL_BIN[command]
    if effective == "uv":
        uv_lock_present = (
            cwd_has_uv_lock() if cwd is None and uv_workspace_root is None else cwd_has_uv_lock(cwd, uv_workspace_root)
        )
        if uv_lock_present and ensure_uv_available():
            return "uv", "uv", ["run", "--frozen", bin_name], None
        executable = _resolve_python_tool_direct(command)
        return "direct", executable, [], "uv->direct"
    if effective == "uvx":
        if ensure_uvx_available():
            return "uvx", "uvx", [bin_name], None
        executable = _resolve_python_tool_direct(command)
        return "direct", executable, [], "uvx->direct"
    if effective == "direct":
        executable = _resolve_python_tool_direct(command)
        return "direct", executable, [], None
    raise ValueError(f"python-runnerの設定値が正しくありません: {effective!r}（許容値: uv, uvx, direct）")


def _resolve_js_commandline(
    command: str,
    config: pyfltr.config.model.Config,
    *,
    effective: str | None = None,
    cwd: pathlib.Path | None = None,
) -> tuple[str, list[str]]:
    bin_name = JS_TOOL_BIN[command]
    runner = effective if effective is not None else config["js-runner"]
    packages: list[str] = list(pyfltr.config.model.command_setting(config.values, command, "packages", []))

    if runner == "pnpx":
        main_spec = _JS_TOOL_PNPX_PACKAGE_SPEC.get(command, bin_name)
        prefix: list[str] = []
        # plugin/rule packageを`--package`で並べる経路に限り、pnpm公式opt-outで
        if packages:
            prefix.append("--config.enableGlobalVirtualStore=false")
        prefix.extend(["--package", main_spec])
        for pkg in packages:
            prefix.extend(["--package", pkg])
        prefix.extend(["dlx", bin_name])
        return "pnpm", prefix
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
        base = cwd if cwd is not None else pathlib.Path.cwd()
        bin_dir = base / "node_modules" / ".bin"
        # Windowsでは `.cmd` 付きのラッパーを優先する。pyrightの静的評価では
        candidates: list[pathlib.Path] = []
        if os.name == "nt":
            candidates.append(bin_dir / f"{bin_name}.cmd")
        candidates.append(bin_dir / bin_name)
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate), []
        raise FileNotFoundError(str(candidates[0]))
    raise ValueError(f"js-runnerの設定値が正しくありません: {runner!r}（許容値: {', '.join(JS_RUNNERS)}）")


# 二重定義による追従漏れを避けるためのSSOT。
_DELEGATE_RUNNER_VALUES: frozenset[str] = frozenset(v for v in COMMAND_RUNNERS if v.endswith("-runner"))
_DIRECT_RUNNER_VALUES: frozenset[str] = frozenset(COMMAND_RUNNERS) - _DELEGATE_RUNNER_VALUES


def resolve_effective_runner(command: str, runner: str, config: pyfltr.config.model.Config) -> str:
    if runner in _DELEGATE_RUNNER_VALUES:
        return str(config[runner])
    if runner in _DIRECT_RUNNER_VALUES:
        return runner
    raise ValueError(f"{command}-runnerの設定値が正しくありません: {runner!r}（許容値: {', '.join(COMMAND_RUNNERS)}）")


_JS_EFFECTIVE_VALUES: frozenset[str] = frozenset(JS_RUNNERS) - {"direct"}


def _resolve_mise_runner_commandline(
    command: str,
    config: pyfltr.config.model.Config,
    runner: str,
    source: str,
    *,
    allow_side_effects: bool,
    cwd: pathlib.Path | None = None,
) -> ResolvedCommandline:
    if command not in _BIN_TOOL_SPEC:
        raise ValueError(
            f"miseバックエンドに `{command}` が登録されていないため、"
            f'`{command}-runner = "mise"` は使用できません。'
            f'`{command}-runner = "python-runner"`（既定）または'
            f' `{command}-runner = "direct"` への切り替えを検討してください'
        )
    spec = _BIN_TOOL_SPEC[command]
    default_version: str = pyfltr.tools.BUILTIN_COMMANDS[command].defaults[f"{command}-version"]
    version = pyfltr.config.model.command_setting(config.values, command, "version", default_version)
    tool_spec_omitted = False
    if ":" in version or "@" in version:
        prefix = ["exec", version, "--", spec.bin_name]
    elif version == default_version and _is_tool_active_in_mise_config(
        command, spec, config, allow_side_effects=allow_side_effects, cwd=cwd
    ):
        prefix = ["exec", "--", spec.bin_name]
        tool_spec_omitted = True
    else:
        tool_name = spec.mise_backend or spec.bin_name
        if command == "lychee" and sys.platform == "linux" and platform.machine().lower() in {"x86_64", "amd64"}:
            tool_name += "[asset_pattern=lychee-x86_64-unknown-linux-musl.tar.gz]"
        prefix = ["exec", f"{tool_name}@{version}", "--", spec.bin_name]
    return ResolvedCommandline(
        executable="mise",
        prefix=prefix,
        runner=runner,
        runner_source=source,
        effective_runner="mise",
        tool_spec_omitted=tool_spec_omitted,
    )


def _resolve_direct_runner_commandline(
    command: str,
    config: pyfltr.config.model.Config,
    runner: str,
    source: str,
    effective: str,
    *,
    cwd: pathlib.Path | None = None,
) -> ResolvedCommandline:
    if command in PACKAGE_MANAGER_TOOL_BIN:
        executable = _resolve_direct_executable(PACKAGE_MANAGER_TOOL_BIN[command])
        return ResolvedCommandline(
            executable=executable,
            prefix=[],
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    if command in _BIN_TOOL_SPEC:
        spec = _BIN_TOOL_SPEC[command]
        executable = _resolve_direct_executable(spec.bin_name)
        return ResolvedCommandline(
            executable=executable,
            prefix=[],
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    if command in PYTHON_TOOL_BIN:
        executable = _resolve_python_tool_direct(command)
        return ResolvedCommandline(
            executable=executable,
            prefix=[],
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    if command in JS_TOOL_BIN:
        executable, prefix = _resolve_js_commandline(command, config, effective="direct", cwd=cwd)
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=effective,
        )
    executable = _resolve_direct_executable(command)
    return ResolvedCommandline(
        executable=executable,
        prefix=[],
        runner=runner,
        runner_source=source,
        effective_runner=effective,
    )


def build_commandline(
    command: str,
    config: pyfltr.config.model.Config,
    *,
    allow_side_effects: bool = False,
    uv_workspace_root: pathlib.Path | None = None,
    cwd: pathlib.Path | None = None,
) -> ResolvedCommandline:
    """ツール起動コマンドラインを構築する（副作用は `allow_side_effects` で制御）。

    `{command}-runner` および `{command}-path` の設定に従い、`mise exec ... --` 形式・
    `pnpm --package ... dlx ...`形式・`uv run --frozen`形式・`uvx <bin>`形式・直接実行（PATH解決）のいずれかを返す。

    `{command}-runner` 値はカテゴリ委譲値（`python-runner` / `js-runner` / `bin-runner`）と
    直接指定値（`direct` / `mise` / `uv` / `uvx` / `pnpx` / `pnpm` / `npm` / `npx` / `yarn`）の2分類で扱い、
    対等な選択肢として並ぶ。
    カテゴリ横断の組み合わせ（例: `mypy-runner = "pnpm"`）はバリデーションで拒否せず、
    無意味な組み合わせは実行時に解決ロジックがエラー終了する（実装簡潔さ優先）。

    解決経路の主要な設計判断は次の通り。

    - `{command}-path` 非空指定は最優先で `effective_runner = "direct"` 固定とする
      （`runner_source = "path-override"`）。利用者が明示したパスを尊重するため
    - `uv` 経路は `uv.lock` 存在を必須とする。`uvx` 経路はlockを参照しない。両者の使い分けは
      利用者プロジェクトの登録版（`uv`）か最新版（`uvx`）かの選択を意図する
    - `direct` フォールバックは `uv`・`uvx`・`mise` の各経路で前提条件を満たさないとき発動し、
      `runner_fallback` に退行経路（例: `"uv->direct"`）を記録する。利用者環境差で起動失敗するより、
      最低限PATH上のバイナリで動くようにするため
    - `mise` 経路はmise設定記述があり `{command}-version` が既定値 `"latest"` のときに限り
      tool spec部分を省略する。利用者がmise設定で固定したcomponents・バージョンを尊重するため

    `allow_side_effects=False`（既定）では `mise exec --version` の事前チェックや
    `mise trust` を行わない。判定関数 `get_mise_active_tools` も副作用OFFで呼び、
    未信頼config由来エラーを「記述なし」扱いとして従来形のtool spec組み立てへフォールバックする。
    `command-info` サブコマンドの `--check` 無し呼び出しから安全に呼べるようにするためである。
    `allow_side_effects=True` 時は判定経路でも `mise-auto-trust` 設定に従いtrust→再呼び出しを許可する。

    ツールが特定できない場合は `FileNotFoundError` を、
    `{command}-runner` 値の組み合わせ自体が不正な場合は `ValueError` を送出する。
    """
    runner, source = resolve_runner(command, config)
    effective = resolve_effective_runner(command, runner, config)

    if pyfltr.config.model.command_setting(config.values, command, "path", "") != "":
        return ResolvedCommandline(
            executable=os.path.expanduser(pyfltr.config.model.command_setting(config.values, command, "path", "")),
            prefix=[],
            runner=runner,
            runner_source="path-override",
            effective_runner="direct",
        )

    if effective == "mise":
        return _resolve_mise_runner_commandline(command, config, runner, source, allow_side_effects=allow_side_effects, cwd=cwd)

    if effective in _JS_EFFECTIVE_VALUES:
        if command not in JS_TOOL_BIN:
            raise RunnerMismatchError(
                command,
                runner,
                effective,
                category="JS系（pnpm・npmなどで起動する）",
                alternatives='"direct" / "mise" / "bin-runner" / "python-runner"',
            )
        markdownlint_environment = None
        if command == "markdownlint" and effective == "pnpx":
            markdownlint_environment = pyfltr.command.markdownlint_env.resolve_environment(
                cwd if cwd is not None else pathlib.Path.cwd()
            )
            if allow_side_effects:
                pyfltr.command.markdownlint_env.ensure_environment(
                    markdownlint_environment,
                    timeout=pyfltr.config.model.resolve_command_timeout(config.values, command),
                )
            executable = markdownlint_environment.executable
            prefix: list[str] = []
        else:
            executable, prefix = _resolve_js_commandline(command, config, effective=effective, cwd=cwd)
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=effective,
            markdownlint_environment=markdownlint_environment,
        )

    if effective in ("uv", "uvx"):
        if command not in PYTHON_TOOL_BIN:
            raise RunnerMismatchError(
                command,
                runner,
                effective,
                category="Python系（uv・uvxで起動する）",
                alternatives='"direct" / "mise" / "bin-runner" / "js-runner"',
            )
        resolved_effective, executable, prefix, runner_fallback = _resolve_python_commandline(
            command,
            effective,
            cwd=cwd,
            uv_workspace_root=uv_workspace_root,
        )
        return ResolvedCommandline(
            executable=executable,
            prefix=prefix,
            runner=runner,
            runner_source=source,
            effective_runner=resolved_effective,
            runner_fallback=runner_fallback,
        )

    return _resolve_direct_runner_commandline(command, config, runner, source, effective, cwd=cwd)


def ensure_mise_available(
    resolved: ResolvedCommandline,
    config: pyfltr.config.model.Config,
    *,
    command: str | None = None,
    cwd: pathlib.Path | None = None,
) -> ResolvedCommandline:
    if resolved.executable != "mise":
        return resolved
    bin_name = resolved.prefix[-1]
    has_tool_spec = len(resolved.prefix) >= 2 and resolved.prefix[1] != "--"
    tool_spec = resolved.prefix[1] if has_tool_spec else ""

    if shutil.which("mise") is None:
        resolved_path = shutil.which(bin_name)
        if resolved_path is None:
            raise FileNotFoundError(bin_name)
        return dataclasses.replace(
            resolved,
            executable=resolved_path,
            prefix=[],
            effective_runner="direct",
            runner_fallback="mise->direct",
        )

    # 解決してしまう挙動を回避するため、PATHからmise toolパスを除外したenvを渡す。
    mise_env = build_mise_subprocess_env(dict(os.environ))
    if has_tool_spec:
        check_args = ["mise", "exec", tool_spec, "--", bin_name, "--version"]
    else:
        check_args = ["mise", "exec", "--", bin_name, "--version"]
    returncode, _stdout, stderr, trust_failed = pyfltr.command.mise.run_mise_with_trust(
        check_args, mise_env, config, allow_side_effects=True, cwd=cwd
    )
    if returncode == 0:
        return resolved
    if trust_failed:
        raise FileNotFoundError(f"{MISE_TRUST_FAILURE_PREFIX}{stderr.strip()}")
    hint_command = command if command is not None else bin_name
    hint = f'{hint_command}-runner = "direct" を指定するとmiseを介さずPATH上のバイナリを直接実行します'
    prefix_text = f"mise exec {tool_spec} -- {bin_name}" if has_tool_spec else f"mise exec -- {bin_name}"
    raise FileNotFoundError(f"{prefix_text}: {stderr.strip()}\n{hint}")
