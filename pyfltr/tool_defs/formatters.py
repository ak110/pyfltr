"""formattersのツール登録。"""

from pyfltr.tool_defs.common import JS_COMMON_TARGETS
from pyfltr.tool_types import BinToolSpec, CommandInfo

COMMANDS: dict[str, CommandInfo] = {
    # formatter群: 純粋formatter先頭（prettier）→ 中段（決定論的整形）→ 末尾（prek→pre-commit）。
    "prettier": CommandInfo(
        type="formatter",
        fixed_cost=1.5,
        per_file_cost=0.02,
        targets=[
            *JS_COMMON_TARGETS,
            "*.vue",
            "*.svelte",
            "*.json",
            "*.jsonc",
            "*.yaml",
            "*.yml",
            "*.md",
            "*.mdx",
            "*.css",
            "*.scss",
            "*.less",
            "*.html",
        ],
        js_bin="prettier",
        language="javascript",
        defaults={
            "prettier": False,
            "prettier-path": "",
            "prettier-runner": "js-runner",
            "prettier-args": [],
            # prettierは--check（read-only）と--write（書き込み）が排他のため、
            # pyfltrは2段階で実行する。詳細はcommand.pyの`execute_prettier_two_step`を参照。
            "prettier-check-args": ["--check"],
            "prettier-write-args": ["--write"],
            "prettier-fast": True,
        },
        execution_kind="prettier",
    ),
    "ruff-format": CommandInfo(
        type="formatter",
        fixed_cost=0.02,
        python_bin="ruff",
        language="python",
        defaults={
            "ruff-format": False,
            "ruff-format-path": "",
            "ruff-format-runner": "python-runner",
            "ruff-format-args": ["format", "--exit-non-zero-on-format"],
            "ruff-format-fast": True,
            # ruff-format実行時にruff check --fix --unsafe-fixesを先に実行するか。
            # 既定では有効とし、未整形のimportソートや安全に自動修正できるlint違反を
            # フォーマットと一緒に片付ける（ruff公式推奨ワークフローの発展形）。
            # unsafe fix採用方針はbiome側（`biome-fix-args`）と統一する。
            # lintエラーは別途ruff-checkで検出される前提のため、ステップ1の
            # lint violation（exit 1）はruff-format側では失敗扱いしない。
            "ruff-format-by-check": True,
            "ruff-format-check-args": ["check", "--fix", "--unsafe-fixes"],
        },
        execution_kind="ruff-format",
        execution_enabled_key="ruff-format-by-check",
    ),
    "uv-sort": CommandInfo(
        type="formatter",
        targets="pyproject.toml",
        fixed_cost=0.2,
        python_bin="uv-sort",
        language="python",
        defaults={
            "uv-sort": False,
            "uv-sort-path": "",
            "uv-sort-args": [],
            "uv-sort-runner": "python-runner",
            "uv-sort-fast": True,
        },
    ),
    # bin-runner対応ツール（formatter → linterの順）
    "shfmt": CommandInfo(
        type="formatter",
        targets="*.sh",
        subproject_aware=False,
        bin_spec=BinToolSpec(bin_name="shfmt"),
        defaults={
            # -- bin-runner対応ツール --
            "shfmt": False,
            "shfmt-path": "",
            "shfmt-runner": "bin-runner",
            "shfmt-args": [],
            # shfmt は prettier 同様の二段階実行。-l でチェック、-w で書き込み。
            "shfmt-check-args": ["-l"],
            "shfmt-write-args": ["-w"],
            "shfmt-version": "latest",
            "shfmt-fast": True,
        },
        execution_kind="check-write",
    ),
    # taplo: Rust製TOMLフォーマッター/リンター。shfmtと同様の2段階実行（check → format）。
    # 既定で無効（opt-in）。
    "taplo": CommandInfo(
        type="formatter",
        targets="*.toml",
        fixed_cost=0.3,
        bin_spec=BinToolSpec(bin_name="taplo"),
        defaults={
            # taplo: Rust製TOMLフォーマッター/リンター。bin-runner経由。既定で無効（opt-in）。
            # shfmtと同様の2段階実行（check → format）。
            "taplo": False,
            "taplo-path": "",
            "taplo-runner": "bin-runner",
            "taplo-args": [],
            "taplo-check-args": ["check"],
            "taplo-write-args": ["format"],
            "taplo-version": "latest",
            "taplo-fast": True,
        },
        execution_kind="check-write",
    ),
    # Rust / .NETツール（formatter）。いずれもpass-filenames=FalseでCrate/solution
    # 全体を対象とするproject-level実行で動作する。serial_groupにより同一ツールチェイン
    # （cargo / dotnet）のコマンドは直列実行され、targetディレクトリ等のロック競合を回避する。
    "cargo-fmt": CommandInfo(
        type="formatter",
        targets="*.rs",
        serial_group="cargo",
        fixed_cost=1.0,
        bin_spec=BinToolSpec(bin_name="cargo", mise_backend="rust"),
        language="rust",
        defaults={
            # -- Rust 言語ツール --
            # いずれも pass-filenames=False で crate 全体を対象とする project-level 実行。
            # 既定で bin-runner 経路を通り、グローバル `bin-runner` 既定 (mise) により mise exec で
            # 解決する。従来挙動 (PATH 上の cargo / cargo-deny を直接実行) を維持したい場合は
            # `cargo-fmt-runner = "direct"` 等の明示指定または `cargo-fmt-path` への明示パス指定で切り替えられる。
            "cargo-fmt": False,
            "cargo-fmt-path": "",
            "cargo-fmt-runner": "bin-runner",
            "cargo-fmt-version": "latest",
            # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
            "cargo-fmt-args": ["fmt"],
            "cargo-fmt-pass-filenames": False,
            "cargo-fmt-fast": True,
        },
    ),
    "dotnet-format": CommandInfo(
        type="formatter",
        targets=["*.cs", "*.csproj", "*.sln", "Directory.Build.props", ".editorconfig"],
        serial_group="dotnet",
        fixed_cost=2.0,
        bin_spec=BinToolSpec(bin_name="dotnet", mise_backend="dotnet"),
        language="dotnet",
        defaults={
            # -- .NET 言語ツール --
            # 既定で bin-runner 経路を通り、グローバル `bin-runner` 既定 (mise) により mise exec で
            # 解決する。従来挙動 (PATH 上の dotnet を直接実行) を維持したい場合は
            # `dotnet-format-runner = "direct"` 等の明示指定または `dotnet-format-path` への
            # 明示パス指定で切り替えられる。directモードでは`DOTNET_ROOT`環境変数配下にdotnet実行ファイルが
            # あれば優先採用する。
            "dotnet-format": False,
            "dotnet-format-path": "",
            "dotnet-format-runner": "bin-runner",
            "dotnet-format-version": "latest",
            # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
            "dotnet-format-args": ["format"],
            "dotnet-format-pass-filenames": False,
            "dotnet-format-fast": True,
        },
    ),
    # prekはRust製の`.pre-commit-config.yaml`実行系。workspace rootから再帰的に
    # サブディレクトリの設定ファイルを探索する仕様のため、既定引数へ--configを明示して
    # workspace探索を無効化する（pyfltr/config/config.pyの`prek`設定ブロックのコメント参照）。
    "prek": CommandInfo(
        type="formatter",
        targets="*",
        config_files=[".pre-commit-config.yaml"],
        subproject_aware=False,
        allows_external_paths=False,
        defaults={
            # prek統合。pre-commitと同一の.pre-commit-config.yamlを読むRust製の実行系。
            # 環境変数・段階的実行・出力書式がpre-commitと一致することをprek 0.4.11で実機検証済み。
            # prekはworkspace rootから再帰的にサブディレクトリの設定ファイルを探索して実行対象へ含めるため、
            # 既定引数へ--configを明示してworkspace探索を無効化する（.prekignoreによる除外は
            # 検証バージョンで機能しなかったため採用しない）。
            "prek": False,
            "prek-path": "prek",
            "prek-runner": "direct",
            "prek-args": ["run", "--config=.pre-commit-config.yaml", "--files"],
            "prek-pass-filenames": True,
            "prek-fast": True,
            "prek-auto-skip": True,
            "prek-skip": [],
        },
        execution_kind="precommit",
    ),
    # pre-commitはリポジトリ固有チェックが幅広く実行されるため、他formatterの修正後に最後で呼ぶ。
    # 変更ファイル指定（--files）で起動するが、各hook内部のtypes・types_or・files・excludeフィルタが
    # ファイル指定起動でも適用されるため、関係するhookのみ部分検証できる。
    "pre-commit": CommandInfo(
        type="formatter",
        targets="*",
        config_files=[".pre-commit-config.yaml"],
        subproject_aware=False,
        allows_external_paths=False,
        defaults={
            # pre-commit統合。有効にするとpyfltr run/ci/fast実行時に
            # pre-commit runを変更ファイル指定（--files <対象>）で内部実行する。
            # 各hookの内部フィルタ（types・types_or・files・exclude）はファイル指定起動でも適用されるため、
            # 関係するhookのみ動作する。pass_filenames=Falseのhook（gitleaks等）はpre-commit側で
            # リポジトリ全体走査になるため、ファイル指定渡しでも従来通り動作する。
            # pre-commit-fast = True（既定）によりfastも統合するため、
            # make format相当の場面でpre-commitを別途呼ぶ必要がなくなる。
            # pre-commit配下からpyfltrが起動された場合はPRE_COMMIT=1
            # 環境変数の検出によりpre-commit統合を自動でスキップする。
            # pre-commitとprekを同時に有効化した場合は_warn_precommit_prek_conflictが警告を発行する。
            "pre-commit": False,
            "pre-commit-path": "pre-commit",
            "pre-commit-runner": "direct",
            # `pre-commit run`の位置引数はhook IDとして解釈されるため、
            # ファイル指定には`--files`フラグの前置が必須。
            # また`--files`へ対象ファイルが渡る既定構成では、引数なしの`pre-commit run`が行う
            # 未ステージ変更の退避・復元（`git stash`相当の作業ツリー操作）は発生しない。
            "pre-commit-args": ["run", "--files"],
            "pre-commit-pass-filenames": True,
            "pre-commit-fast": True,
            # .pre-commit-config.yamlからpyfltr関連hookを自動検出してSKIPする
            "pre-commit-auto-skip": True,
            # SKIP環境変数に渡すhook IDの手動指定リスト（auto-skipと併用可能）
            "pre-commit-skip": [],
        },
        execution_kind="precommit",
    ),
}
