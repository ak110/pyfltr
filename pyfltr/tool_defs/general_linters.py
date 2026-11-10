"""general_lintersのツール登録。"""

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_types import BinToolSpec, CommandInfo, FunctionRef, StructuredOutputSpec

COMMANDS: dict[str, CommandInfo] = {
    "ec": CommandInfo(
        type="linter",
        targets="*",
        bin_spec=BinToolSpec(
            bin_name="editorconfig-checker",
            mise_backend="github:editorconfig-checker/editorconfig-checker",
        ),
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*\w+:\s*(?P<message>.+)",
        defaults={
            "ec": False,
            "ec-path": "",
            "ec-runner": "bin-runner",
            "ec-args": ["-format", "gcc", "-no-color"],
            "ec-version": "latest",
            "ec-fast": True,
        },
    ),
    "shellcheck": CommandInfo(
        type="linter",
        targets="*.sh",
        per_file_cost=0.03,
        subproject_aware=False,
        bin_spec=BinToolSpec(bin_name="shellcheck"),
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*\w+:\s*(?P<message>.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_shellcheck_json"),
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_shellcheck_url"),
        structured_output=(
            "shellcheck-json",
            StructuredOutputSpec(
                inject=["-f", "json"],
                conflicts=["-f"],
            ),
        ),
        defaults={
            "shellcheck-json": True,
            "shellcheck": False,
            "shellcheck-path": "",
            "shellcheck-runner": "bin-runner",
            "shellcheck-args": ["-f", "gcc"],
            "shellcheck-version": "latest",
            "shellcheck-fast": True,
        },
    ),
    "typos": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=0.04,
        per_file_cost=0.007,
        subproject_aware=False,
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_typos_jsonl"),
        structured_output=(
            "typos-json",
            StructuredOutputSpec(
                inject=["--format=json"],
                conflicts=["--format"],
            ),
        ),
        defaults={
            "typos-json": True,
            "typos": False,
            "typos-path": "",
            "typos-runner": "direct",
            "typos-args": ["--format", "brief"],
            "typos-version": "latest",
            "typos-fast": True,
        },
    ),
    "actionlint": CommandInfo(
        type="linter",
        targets=[".github/workflows/*.yaml", ".github/workflows/*.yml"],
        fixed_cost=0.2,
        bin_spec=BinToolSpec(bin_name="actionlint"),
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+?)(?:\s*\[(?P<rule>[^\[\]\s]+)\])?\s*$",
        defaults={
            "actionlint": False,
            "actionlint-path": "",
            "actionlint-runner": "bin-runner",
            "actionlint-args": [],
            "actionlint-version": "latest",
            "actionlint-fast": True,
        },
    ),
    # pinact: GitHub Actionsの`uses:`がSHAと版コメントでピン留めされているかを検査する。
    # 既定引数の`--no-api`によりGitHub APIを呼ばない構文上の検査に限る。
    "pinact": CommandInfo(
        type="linter",
        targets=[".github/workflows/*.yaml", ".github/workflows/*.yml"],
        fixed_cost=0.1,
        bin_spec=BinToolSpec(bin_name="pinact"),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_pinact_sarif"),
        defaults={
            # pinact: `--no-api`でGitHub APIを呼ばない構文上の検査（40文字SHAと版コメントの有無）に限定し、
            # `GITHUB_TOKEN`を不要にする。`--format sarif`の指定時は`--fix`が既定で無効となりファイルを書き換えない。
            # `--check`は`--no-api`と併用すると終了コード3で失敗するため含めない。
            # 自動修正（タグからSHAへの解決）はGitHub APIを要し結果が実行時点の最新リリースで変わるため、
            # `pinact-fix-args`は定義せずfix段へ載せない。修正は利用者が`pinact run`を直接実行する。
            "pinact": False,
            "pinact-path": "",
            "pinact-runner": "bin-runner",
            "pinact-args": ["run", "--no-api", "--format", "sarif"],
            "pinact-version": "latest",
            "pinact-fast": True,
        },
    ),
    # GitLab CI設定の構文検証。GitLab API経由でlintするためネットワーク・認証が必須。
    # 既定で無効（opt-in）とし、CIや初学者環境で誤って失敗しないようにする。
    "glab-ci-lint": CommandInfo(
        type="linter",
        targets=".gitlab-ci.yml",
        fixed_cost=1.0,
        bin_spec=BinToolSpec(bin_name="glab"),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_glab_ci_lint"),
        defaults={
            # glab ci lint は GitLab API 経由で .gitlab-ci.yml を検証する。
            # 認証・ネットワーク必須のため既定で無効 (opt-in)。
            # サブコマンド `ci lint` は args 既定値として持たせ、明示 path 指定経路でも適用されるようにする。
            "glab-ci-lint": False,
            "glab-ci-lint-path": "",
            "glab-ci-lint-runner": "bin-runner",
            "glab-ci-lint-args": ["ci", "lint"],
            "glab-ci-lint-version": "latest",
            "glab-ci-lint-fast": False,
        },
        execution_kind="glab",
    ),
    # yamllint: Python製YAMLリンター。YAML全般を対象とする。既定で無効（opt-in）。
    "yamllint": CommandInfo(
        type="linter",
        targets=["*.yaml", "*.yml"],
        fixed_cost=0.1,
        per_file_cost=0.01,
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*\[(?P<severity>error|warning)\]"
        r"\s+(?P<message>.+?)(?:\s+\((?P<rule>[^()\s]+)\))?\s*$",
        structured_output=(
            "yamllint-parsable",
            StructuredOutputSpec(
                # 既定の`-f auto`はGitHub Actions上で`::error`形式へ切り替わり、
                # 標準形式は見出し行と字下げした違反行の複数行となるため、
                # 環境によらず1違反1行となる`parsable`へ固定する。
                inject=["-f", "parsable"],
                conflicts=["-f", "--format"],
            ),
        ),
        defaults={
            "yamllint-parsable": True,
            # yamllint: Python製YAMLリンター。既定で無効（opt-in）。直接実行経路。
            "yamllint": False,
            "yamllint-path": "",
            "yamllint-runner": "direct",
            "yamllint-args": [],
            "yamllint-fast": True,
        },
    ),
    # hadolint: Haskell製Dockerfileリンター。bin-runner経由。既定で無効（opt-in）。
    "hadolint": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_hadolint_json"),
        structured_output=("hadolint-structured", StructuredOutputSpec(inject=["--format=json"], conflicts=["--format", "-f"])),
        type="linter",
        targets=["Dockerfile", "Dockerfile.*", "*.Dockerfile"],
        fixed_cost=0.2,
        per_file_cost=0.05,
        bin_spec=BinToolSpec(bin_name="hadolint"),
        defaults={
            "hadolint-structured": True,
            # hadolint: Dockerfile専用リンター。bin-runner経由。既定で無効（opt-in）。
            "hadolint": False,
            "hadolint-path": "",
            "hadolint-runner": "bin-runner",
            "hadolint-args": [],
            "hadolint-version": "latest",
            "hadolint-fast": True,
        },
    ),
    # gitleaks: Goバイナリ。リポジトリ全体のシークレット検出。
    # pass-filenames=Falseで全体を対象とする。bin-runner経由。既定で無効（opt-in）。
    "gitleaks": CommandInfo(
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.native", "parse_gitleaks_json"),
        structured_output=(
            "gitleaks-structured",
            StructuredOutputSpec(
                inject=["--report-format=json", "--report-path=-", "--redact"],
                conflicts=["--report-format", "--report-path", "--redact"],
            ),
        ),
        type="linter",
        targets="*",
        fixed_cost=1.0,
        allows_external_paths=False,
        bin_spec=BinToolSpec(bin_name="gitleaks"),
        defaults={
            "gitleaks-structured": True,
            # gitleaks: シークレット検出ツール（Goバイナリ）。bin-runner経由。既定で無効（opt-in）。
            # `detect` サブコマンドは args 既定値として持たせる（glab-ci-lint と同じ設計）。
            # pass-filenames=false でリポジトリ全体を対象とする。
            "gitleaks": False,
            "gitleaks-path": "",
            "gitleaks-runner": "bin-runner",
            "gitleaks-args": ["detect", "--no-banner"],
            "gitleaks-pass-filenames": False,
            "gitleaks-version": "latest",
            "gitleaks-fast": False,
        },
    ),
    # semgrep: Python製多言語SASTツール。ルールセット指定が必須のため既定で無効（opt-in）。
    # 利用者が`semgrep-args`で`--config=auto`または個別ルールセットを指定する前提。
    # 対象は多言語のため`*`globで広く対象とし、ツール側のルールセットで実際の検査対象を限定する。
    # `pass-filenames=True`（既定）でファイル一覧を末尾引数として渡す。
    # gitleaksのようなリポジトリ全体走査ではなく対象ファイル群を明示する設計とし、
    # pyfltr側のexclude / .gitignore尊重の効果をそのまま反映する。
    "semgrep": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=2.0,
        per_file_cost=0.05,
        allows_external_paths=False,
        python_bin="semgrep",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_semgrep_json"),
        defaults={
            # semgrep: 多言語SAST。ルールセット指定が必須のため既定で無効（opt-in）。
            # semgrepは`mcp`を厳密ピンし`click`にも強い制約を課すため本体依存から外した。
            # `uvx`既定により別環境で解決し、pyfltrの依存グラフから切り離す。
            # 利用者が版を固定したい場合は`semgrep-path`または`semgrep-runner`で上書きする。
            # 既定argsは空とする（ルールセット既定はsemgrep側の意図と衝突するため）。
            # 利用者は`semgrep-args = ["scan", "--json", "--error", "--config=auto"]`等で
            # サブコマンド・出力形式・ルールセットをまとめて指定する。
            "semgrep": False,
            "semgrep-path": "",
            "semgrep-runner": "uvx",
            "semgrep-args": [],
            "semgrep-fast": False,
        },
    ),
    # bandit: Python専用source-level SAST。既定で無効（opt-in）。
    # YAML/TOML形式設定の自動読み込みはbandit本体にないため`--configfile <設定ファイル>`明示が必須。
    # 起点cwd直下のpyproject.toml / .bandit.yaml / .bandit.tomlを順に探索し、
    # 最初に見つかったファイルを`--configfile <絶対パス>`形式で注入する。
    # `.bandit`はINI形式で`--configfile`の対象外のためbandit本体の`--recursive`時自動探索に委ねる。
    "bandit": CommandInfo(
        type="linter",
        targets="*.py",
        fixed_cost=0.3,
        per_file_cost=0.05,
        config_arg_template=["--configfile", "{path}"],
        config_inject_candidates=[
            "pyproject.toml",
            ".bandit.yaml",
            ".bandit.toml",
        ],
        python_bin="bandit",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_bandit_json"),
        defaults={
            # bandit: Python専用source-level SAST。既定で無効（opt-in）。
            # `--quiet --recursive --format=json`で実行し、JSON出力をパースする。
            # 設定ファイル（pyproject.toml / .bandit.yaml / .bandit.toml）はbandit本体が自動読み込みしないため、
            # CommandInfoのconfig_arg_template経由で`--configfile <絶対パス>`を注入する。
            "bandit": False,
            "bandit-path": "",
            "bandit-runner": "python-runner",
            "bandit-args": ["--quiet", "--recursive", "--format=json"],
            "bandit-fast": False,
        },
    ),
}
