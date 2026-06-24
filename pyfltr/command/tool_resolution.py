"""ツール解決失敗ハンドリング。

ツール解決失敗時の利用者向け文面組み立てと`CommandResult`生成、uv経路でのツール未登録警告を担う。
`pyfltr.command.runner`から送出される`FileNotFoundError`のcatch側として動作する。
"""

import pyfltr.command.runner
import pyfltr.config.config
import pyfltr.warnings_
from pyfltr.command.core_ import CommandResult


def format_tool_resolution_failure(
    command: str,
    raw_identifier: str,
    config: pyfltr.config.config.Config,
) -> str:
    """ツール解決失敗時の利用者向け文面を組み立てる。

    runner.pyの`FileNotFoundError`は識別子（bin名・パス・mise tool spec等）のみを保持する契約で、
    本ヘルパーがcatch側として「探索経路」「代替手段（runner切り替え・パス明示）」を併記する。

    分類:

    - パッケージマネージャーのサブコマンド系（`PACKAGE_MANAGER_TOOL_BIN`）:
      対象のパッケージマネージャー（`uv` / `pnpm` / `npm` / `yarn`）導入または `{command}-path` 明示を案内する
    - Python系（`PYTHON_TOOL_BIN`）: `uv` / `uvx` への切り替えまたは `{command}-path` 明示を案内する
    - JS系（`JS_TOOL_BIN`）でdirect指定: `node_modules` 探索失敗として `pnpm install` / `pnpx` 切り替えを案内する
    - JS系（`JS_TOOL_BIN`）でdirect以外: PATH探索失敗として `{command}-path` 明示を案内する
    - ネイティブ系（`_BIN_TOOL_SPEC`）: `direct` への切り替えまたは `{command}-path` 明示を案内する
    - その他（カスタムコマンド等）: 汎用案内（PATH探索失敗）
    """
    # 効果値（resolve_effective_runner適用後）を判定し、direct分岐との切り分けに使う。
    effective_runner: str | None = None
    try:
        runner_value, _ = pyfltr.command.runner.resolve_runner(command, config)
        effective_runner = pyfltr.command.runner.resolve_effective_runner(command, runner_value, config)
    except ValueError:
        effective_runner = None

    if command in pyfltr.command.runner.PACKAGE_MANAGER_TOOL_BIN:
        # uv audit等は既定でdirect解決のため、PYTHON_TOOL_BIN分岐のuv経由起動案内は誤誘導になる。
        # 起動対象のパッケージマネージャー導入を促す専用文面を返す。
        bin_name = pyfltr.command.runner.PACKAGE_MANAGER_TOOL_BIN[command]
        return (
            f"ツールが見つかりません: パッケージマネージャー `{bin_name}` が PATH 上にありません。"
            f"`{bin_name}` を導入するか、`{command}-path` で実行ファイルを明示してください"
        )
    if command in pyfltr.command.runner.PYTHON_TOOL_BIN:
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
    # ネイティブ系（mise経路の事前チェック失敗）は`ensure_mise_available`が
    # mise stderrとhint文を改行区切りで連結した文面を例外引数に保持する契約。
    # 当該複数行文面は素通し採用し、mise stderrを欠落させない（runner.pyのmodule docstring参照）。
    # `mise trust`失敗時の単行文面（`mise trust --yes --all: <stderr>`）は本ヘルパーの
    # 末尾分岐の汎用文面組み立てを通過させ、`mise trust`プレフィクスで原因種別を利用者へ伝える。
    if "\n" in raw_identifier:
        return f"ツールが見つかりません: {raw_identifier}"
    return (
        f"ツールが見つかりません: `{raw_identifier}` が解決できません。"
        f'`{command}-runner = "direct"` への切り替えか、`{command}-path` で実行ファイルを明示してください'
    )


def failed_resolution_result(
    command: str,
    command_info: pyfltr.config.config.CommandInfo,
    message: str,
    *,
    files: int,
    hint: str | None = None,
) -> CommandResult:
    """ツール解決失敗時の `CommandResult` を組み立てる。

    `files` には実際の処理対象件数を渡す。`status` は `resolution_failed` を返し、
    通常の実行失敗（`failed`）と区別できるようにする。
    `hint` を指定すると `emit_warning` 経由で利用者向けの追加案内を併記する。
    """
    pyfltr.warnings_.emit_warning(source="tool-resolve", message=f"{command}: {message}", hint=hint)
    return CommandResult.from_run(
        command=command,
        command_info=command_info,
        commandline=[],
        returncode=1,
        has_error=True,
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
    """uv / uvx経路でツール未登録の出力を検出した場合に登録手順の案内警告を発行する。

    Python系ツール一式は本体依存に同梱されているため `uvx pyfltr` 単発で動作するが、
    利用者プロジェクトに `uv.lock` が存在すると既定の `python-runner = "uv"` により
    `uv run --frozen <bin>` 経由でプロジェクトのvenvに登録されたツールを呼び出す。
    プロジェクト側に未登録の場合は `uv run` がエラーで失敗するため、登録手順を案内する。
    `uvx` 経路でも同種のエラー出力（`does not have ... as a dependency`）が出る可能性は薄いが、
    判定ルールを揃える方針で同じ案内を発行する。
    """
    if result.effective_runner not in {"uv", "uvx"}:
        return
    if result.returncode is None or result.returncode == 0:
        return
    if not any(pattern in result.output for pattern in _UV_TOOL_MISSING_PATTERNS):
        return
    pyfltr.warnings_.emit_warning(
        source="tool-resolve",
        message=(
            f"{result.command}: {result.effective_runner}経路でのツール起動に失敗しました。"
            "利用者プロジェクトに当該ツールが未登録の可能性があります。"
        ),
        hint=(
            '`uv add --dev "pyfltr[python]"` でPython系ツール一式をdev依存に追加してください。'
            f' 当該ツールを利用者プロジェクトで使わない場合は `{result.command}-runner = "direct"` への切り替えで回避できます。'
        ),
    )
