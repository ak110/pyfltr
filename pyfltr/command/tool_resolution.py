import pyfltr.command.runner
import pyfltr.config.config
import pyfltr.config.model
import pyfltr.tool_types
import pyfltr.tools
import pyfltr.warnings_
from pyfltr.command.core_ import CommandResult


def _uses_uvx_by_default(command: str) -> bool:
    return pyfltr.config.model.command_setting(pyfltr.config.model.DEFAULT_CONFIG, command, "runner", None) == "uvx"


def format_tool_resolution_failure(
    command: str,
    raw_identifier: str,
    config: pyfltr.config.model.Config,
) -> str:
    effective_runner: str | None = None
    try:
        runner_value, _ = pyfltr.command.runner.resolve_runner(command, config)
        effective_runner = pyfltr.command.runner.resolve_effective_runner(command, runner_value, config)
    except ValueError:
        effective_runner = None

    if command in pyfltr.command.runner.PACKAGE_MANAGER_TOOL_BIN:
        bin_name = pyfltr.command.runner.PACKAGE_MANAGER_TOOL_BIN[command]
        return (
            f"ツールが見つかりません: パッケージマネージャー `{bin_name}` が PATH 上にありません。"
            f"`{bin_name}` を導入するか、`{command}-path` で実行ファイルを明示してください"
        )
    if command in pyfltr.command.runner.PYTHON_TOOL_BIN:
        if _uses_uvx_by_default(command):
            return (
                f"ツールが見つかりません: uvx既定のPython製ツール `{raw_identifier}` が PATH 上にありません。"
                f"`uv add --dev {command}`で利用者プロジェクトへ個別に追加するか、"
                f'`{command}-runner = "direct"`へ切り替えるか、'
                f"`{command}-path`で実行ファイルを明示してください"
            )
        return (
            f"ツールが見つかりません: Python系ツール `{raw_identifier}` が PATH 上にありません。"
            f'`{command}-runner = "uv"`（cwdに uv.lock が必要、`uv add --dev "pyfltr[python]"` で依存追加）'
            f' または `{command}-runner = "uvx"` への切り替え、'
            f"もしくは `{command}-path` で実行ファイルを明示してください"
        )
    if command in pyfltr.command.runner.JS_TOOL_BIN:
        if effective_runner == "direct":
            return (
                f"js-runner=direct で `{command}` がローカル node_modules に見つかりません（探索先: {raw_identifier}）。"
                "`pnpm install` などで対象パッケージを導入するか、"
                f'`{command}-runner = "pnpx"` でグローバルキャッシュ経由に切り替えてください'
            )
        return (
            f"ツールが見つかりません: JS系ツール `{raw_identifier}` の解決に失敗しました。"
            f"`pnpm install` などで対象パッケージを導入するか、`{command}-path` で実行ファイルを明示してください"
        )
    if raw_identifier.startswith(pyfltr.command.runner.MISE_TRUST_FAILURE_PREFIX):
        stderr = raw_identifier.removeprefix(pyfltr.command.runner.MISE_TRUST_FAILURE_PREFIX)
        return (
            f"mise設定の信頼（`mise trust --yes --all`）の自動実行に失敗したため、{command}を起動できません: {stderr}。"
            "`mise trust`を手動で実行するか、`mise-auto-trust`の設定を確認するか、"
            f'`{command}-runner = "direct"`を指定してPATH上の実行ファイルを直接起動してください'
        )
    if "\n" in raw_identifier:
        return f"ツールが見つかりません: {raw_identifier}"
    return (
        f"ツールが見つかりません: `{raw_identifier}` が解決できません。"
        f'`{command}-runner = "direct"` への切り替えか、`{command}-path` で実行ファイルを明示してください'
    )


def failed_resolution_result(
    command: str,
    command_info: pyfltr.tool_types.CommandInfo,
    message: str,
    *,
    files: int,
    hint: str | None = None,
) -> CommandResult:
    pyfltr.warnings_.emit_warning(
        source="tool-resolve",
        message=f"{command}: {message}",
        hint=hint
        or f"`pyfltr command-info {command} --check`（MCPではcommand_infoのcheck=True）で解決設定を確認してください。",
    )
    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=[],
        returncode=1,
        files=files,
        output=message,
        elapsed=0.0,
        resolution_failed=True,
    )


_UV_TOOL_MISSING_PATTERNS: tuple[str, ...] = ("does not have",)
"""uv経路でツール未登録を示す統合済みstdout（stderr統合済み）の検出パターン。

`uv run --frozen <bin>` 実行時に利用者プロジェクトへ対象パッケージが未登録だと、
`error: project '<name>' does not have '<pkg>' as a dependency` のメッセージが出る。
`pyfltr/command/process.py` の `run_subprocess` が `stderr=subprocess.STDOUT` で統合するため、
判定対象は `proc.stdout` 由来の `CommandResult.output` になる。
"""


def maybe_emit_uv_missing_tool_warning(result: CommandResult) -> None:
    if result.effective_runner not in {"uv", "uvx"}:
        return
    if result.returncode is None or result.returncode == 0:
        return
    if not any(pattern in result.output for pattern in _UV_TOOL_MISSING_PATTERNS):
        return
    if _uses_uvx_by_default(result.command):
        hint = f'`uvx {result.command}`で直接実行するか、`{result.command}-runner = "direct"`へ切り替えてください。'
    else:
        hint = (
            '`uv add --dev "pyfltr[python]"` でPython系ツール一式をdev依存に追加してください。'
            f" {result.command}を利用者プロジェクトで使わない場合は"
            f' `{result.command}-runner = "direct"` への切り替えで回避できます。'
        )
    pyfltr.warnings_.emit_warning(
        source="tool-resolve",
        message=(
            f"{result.command}: {result.effective_runner}経路でのツール起動に失敗しました。"
            "利用者プロジェクトにこのツールが未登録の可能性があります。"
        ),
        hint=hint,
    )
