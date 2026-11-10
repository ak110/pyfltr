"""auditのツール登録。"""

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_types import CommandInfo, FunctionRef

COMMANDS: dict[str, CommandInfo] = {
    # 依存の脆弱性監査ツール群（uv audit / pnpm audit / npm audit / yarn audit）。
    # いずれもパッケージマネージャー自体のサブコマンドを起動する（実体の解決は
    # runner.pyの`PACKAGE_MANAGER_TOOL_BIN`。`{command}-runner = "direct"`既定）。
    # マニフェスト（pyproject.toml / package.json）の存在をトリガーとし、対象ファイルは
    # 渡さず（`{command}-pass-filenames = false`）プロジェクト単位で起動する。
    # 既定で無効（opt-in）。外部脆弱性データベースへの問い合わせで所要時間を見積もれないため
    # fastは無効（`{command}-fast = false`）。`allows_external_paths=False`で
    # リポジトリ外マニフェストを対象外とする。
    "uv-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="pyproject.toml",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="uv",
        minimum_version=(
            (0, 11, 2),
            "0.11.2未満のuvは脆弱性の検出を終了コードへ反映しないため、未検査または未解消の状態が成功として扱われる",
        ),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_uv_audit"),
        defaults={
            # 依存の脆弱性監査ツール群（uv audit / pnpm audit / npm audit / yarn audit）。
            # BUILTIN_COMMANDS登録順・lintエイリアス・order.mdに合わせ、sqlfluffの直後へ配置する。
            # いずれもパッケージマネージャー自体のサブコマンドを直接呼ぶ（`{command}-runner = "direct"`、
            # 実体の解決はrunner.pyの`PACKAGE_MANAGER_TOOL_BIN`）。ネットワーク必須かつ結果が
            # 外部脆弱性データベース更新で変動するため既定で無効（opt-in）・fast無効。
            # `pass-filenames = false`で対象ファイルは渡さずプロジェクト単位で実行する。
            # bin-runner系ではないため`{command}-version`キーは設けない。
            # uv-auditは`--frozen`でロック解決によるuv.lock書き換えを防ぐ（linterは検査のみで副作用を持たない原則）。
            # `uv audit`はuv側の実験的機能であり、既定では実行のたびに実験的機能である旨の警告を標準エラーへ出力する。
            # 警告は監査結果と無関係でCIログの可読性を下げるため、`--preview-features audit`で抑止する。
            # 抑止により実験的機能である旨の注意喚起が利用者の目に触れなくなるため、
            # uv側の仕様変更で監査の挙動が変わる可能性がある点をガイドの版要件記述で補う。
            "uv-audit": False,
            "uv-audit-path": "",
            "uv-audit-runner": "direct",
            "uv-audit-args": ["audit", "--preview-features", "audit", "--frozen", "--no-progress"],
            "uv-audit-pass-filenames": False,
            "uv-audit-fast": False,
        },
    ),
    "pnpm-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="package.json",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="pnpm",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_pnpm_audit_json"),
        defaults={
            "pnpm-audit": False,
            "pnpm-audit-path": "",
            "pnpm-audit-runner": "direct",
            "pnpm-audit-args": ["audit", "--json"],
            "pnpm-audit-pass-filenames": False,
            "pnpm-audit-fast": False,
        },
    ),
    "npm-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="package.json",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="npm",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_npm_audit_json"),
        defaults={
            "npm-audit": False,
            "npm-audit-path": "",
            "npm-audit-runner": "direct",
            "npm-audit-args": ["audit", "--json"],
            "npm-audit-pass-filenames": False,
            "npm-audit-fast": False,
        },
    ),
    "yarn-audit": CommandInfo(
        aliases=("audit",),
        type="linter",
        targets="package.json",
        fixed_cost=3.0,
        allows_external_paths=False,
        package_manager_bin="yarn",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_yarn_audit_jsonl"),
        defaults={
            # yarn classic（1.x）はJSON Lines（auditAdvisory / auditSummary行）を出力する。
            # yarn berry（2+）は`yarn npm audit --json`等とサブコマンド体系が異なるため、
            # 利用時は`yarn-audit-args`で上書きする。
            "yarn-audit": False,
            "yarn-audit-path": "",
            "yarn-audit-runner": "direct",
            "yarn-audit-args": ["audit", "--json"],
            "yarn-audit-pass-filenames": False,
            "yarn-audit-fast": False,
        },
    ),
}
