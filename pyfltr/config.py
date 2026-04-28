"""設定関連の処理。"""

import copy
import dataclasses
import pathlib
import re
import tomllib
import typing

from pyfltr.builtin_commands import (
    AUTO_ARGS,
    BIN_RUNNERS,
    BUILTIN_COMMAND_NAMES,
    BUILTIN_COMMANDS,
    COMMAND_RUNNERS,
    DOTNET_COMMANDS,
    JAVASCRIPT_COMMANDS,
    JS_RUNNERS,
    LANGUAGE_CATEGORIES,
    PYTHON_COMMANDS,
    REMOVED_COMMANDS,
    RUST_COMMANDS,
    CommandInfo,
    CommandType,
)
from pyfltr.presets import _PRESETS, _REMOVED_PRESETS

# 公開APIとして再エクスポートする定数・型
# （既存のimport経路`pyfltr.config.BUILTIN_COMMANDS`等を維持するため）
__all__ = [
    "AUTO_ARGS",
    "BIN_RUNNERS",
    "BUILTIN_COMMAND_NAMES",
    "BUILTIN_COMMANDS",
    "COMMAND_RUNNERS",
    "DOTNET_COMMANDS",
    "JAVASCRIPT_COMMANDS",
    "JS_RUNNERS",
    "LANGUAGE_CATEGORIES",
    "PYTHON_COMMANDS",
    "REMOVED_COMMANDS",
    "RUST_COMMANDS",
    "CommandInfo",
    "CommandType",
    "Config",
    "DEFAULT_CONFIG",
    "create_default_config",
    "filter_fix_commands",
    "generate_config_text",
    "load_config",
    "resolve_aliases",
]


DEFAULT_CONFIG: dict[str, typing.Any] = {
    # プリセット
    "preset": "",
    # 言語カテゴリキー: presetが示す言語別ツールを通過させるgateとして働く。
    # Trueならpreset由来で有効化された該当カテゴリのコマンドをそのまま通し、
    # False（既定）ならpresetでTrueになった該当コマンドを個別指定がない限り
    # Falseに押し戻す。カテゴリキー単独では何も有効化されない（presetか個別
    # `{command} = true`が必要）。v3.0.0で既定値をFalse（opt-in）に統一。
    # 対象外プロジェクトで言語別linterが勝手に走るのを防ぐためで、Python系は
    # 別途`pip install pyfltr[python]`で依存を導入する必要がある。JavaScript /
    # Rust / .NET系はそれぞれのツールチェインを前提とする。
    "python": False,
    "javascript": False,
    "rust": False,
    "dotnet": False,
    # pre-commit統合。有効にするとpyfltr run/ci/fast実行時に
    # pre-commit run --all-filesを内部で呼び出す。
    # pre-commit-fast = True（既定）によりfastも統合するため、
    # make format相当の場面でpre-commitを別途呼ぶ必要がなくなる。
    # pre-commit配下からpyfltrが起動された場合はPRE_COMMIT=1
    # 環境変数の検出によりpre-commit統合を自動でスキップする。
    "pre-commit": False,
    "pre-commit-path": "pre-commit",
    "pre-commit-runner": "direct",
    "pre-commit-args": ["run", "--all-files"],
    "pre-commit-pass-filenames": False,
    "pre-commit-fast": True,
    # .pre-commit-config.yamlからpyfltr関連hookを自動検出してSKIPする
    "pre-commit-auto-skip": True,
    # SKIP環境変数に渡すhook IDの手動指定リスト（auto-skipと併用可能）
    "pre-commit-skip": [],
    # 自動オプション: 各ツールの望ましい引数を自動挿入する。
    # *-argsとは独立して動作し、重複排除される。Falseで無効化可能。
    "pylint-pydantic": True,
    "mypy-unused-awaitable": True,
    # 構造化出力: 対応ツールの出力形式をJSON等に切り替え、パーサーで
    # ルールコード・severity・fix情報を構造化して取得する。
    # *-argsとは独立した経路で注入されるためpyproject.tomlの上書きに影響されない。
    "ruff-check-json": True,
    "pylint-json": True,
    "pyright-json": True,
    "pytest-tb-line": True,
    "shellcheck-json": True,
    "textlint-json": True,
    "typos-json": True,
    "eslint-json": True,
    "biome-json": True,
    # textlint / markdownlintの起動方式。
    # textlint-path / markdownlint-pathが空のときに、以下の値に従って
    # 実際の起動コマンドを組み立てる。
    # - pnpx: グローバル / キャッシュから実行（既定。従来互換）
    # - pnpm: pnpm exec <cmd>（プロジェクトのnode_modulesを利用）
    # - npm:  npm exec --no -- <cmd>
    # - npx:  npx --no-install -- <cmd>
    # - yarn: yarn run <cmd>
    # - direct: node_modules/.bin/<cmd>を直接起動
    "js-runner": "pnpx",
    # ネイティブバイナリツール（Go/Rust/Haskell製等）の起動方式:
    # - mise: mise exec <tool>@<version> -- <cmd>（既定）
    # - direct: PATH上のバイナリを直接実行
    "bin-runner": "mise",
    # mise実行時に対象ディレクトリのconfigが未信頼だった場合、
    # 自動で`mise trust --yes --all`を実行して再試行するか。
    # worktreeやdotfiles配下などmise.tomlが未信頼扱いになりやすい
    # 環境での手動介入を不要にするためのopt-out設定（既定は有効）。
    "mise-auto-trust": True,
    # コマンド毎に有効無効、パス、追加の引数を設定
    # 言語カテゴリ（python / javascript / rust / dotnet）に属するツールはv3.0.0で
    # opt-in化したため、既定値はFalse。presetで推奨ツールがTrueになり、
    # カテゴリキー（`python = true`等）がgateを開けて有効化を通す構造。
    # presetを使わず個別に`{command} = true`を指定するとgateを越えて最優先で有効化される。
    "mypy": False,
    "mypy-path": "mypy",
    "mypy-args": [],
    "mypy-runner": "direct",
    "mypy-fast": False,
    "pylint": False,
    "pylint-path": "pylint",
    "pylint-args": [],
    "pylint-runner": "direct",
    "pylint-fast": False,
    "pyright": False,
    "pyright-path": "pyright",
    "pyright-args": [],
    "pyright-runner": "direct",
    "pyright-fast": False,
    "ty": False,
    "ty-path": "ty",
    "ty-args": ["check", "--output-format", "concise", "--error-on-warning"],
    "ty-runner": "direct",
    "ty-fast": True,
    "markdownlint": False,
    # pathが空文字の場合は{command}-runner設定（既定 "js-runner"）に基づいて自動解決する。
    # ユーザーが明示的にpathを設定した場合はその値をそのまま使い、args先頭に自動prefixを追加しない。
    "markdownlint-path": "",
    "markdownlint-args": [],
    "markdownlint-runner": "js-runner",
    "markdownlint-fast": True,
    # fixステージ（pyfltr run / fastの自動修正段）で通常argsの後に追加する引数。
    # markdownlint-cli2は--fixでファイルをin-place修正する。
    "markdownlint-fix-args": ["--fix"],
    "textlint": False,
    # pathが空文字の場合は{command}-runner設定（既定 "js-runner"）に基づいて自動解決する。
    "textlint-path": "",
    "textlint-runner": "js-runner",
    # lint / fix共通で常に付与される引数。lint専用オプション（--formatなど）はここではなく
    # textlint-lint-argsに書くこと。fix時は@textlint/fixer-formatterが使用されるが
    # compactフォーマッタが存在しないため、--format compactを共通argsに含めるとfixが失敗する。
    "textlint-args": [],
    # 非fixモード（およびfixモードの後段lintチェック）でのみ付与する引数。
    # 既定はcompactフォーマッタ指定（builtinパーサがcompact出力をパースする前提のため）。
    "textlint-lint-args": ["--format", "compact"],
    # textlint向けルール / プリセットパッケージの列挙。pnpx / npxモードでは
    # --package / -p展開される。pnpm / npm / yarn / directモードでは
    # package.json側で管理する前提のため無視される。
    "textlint-packages": [
        "textlint-rule-preset-ja-technical-writing",
        "textlint-rule-preset-jtf-style",
        "textlint-rule-ja-no-abusage",
    ],
    "textlint-fast": True,
    # fixモード時に通常argsの後に追加する引数。
    # textlintは--fixでautofix可能なルールをin-place修正する。
    "textlint-fix-args": ["--fix"],
    # fixモード実行で「破損させてはならない識別子」を列挙する。
    # preset-jtf-styleの「半角ピリオド→全角句点」ルール等は、コードブロック外にある
    # `.NET` / `Node.js`等の識別子まで変換してしまうことがあるため、
    # fix前には含まれていた識別子がfix後に失われたケースを検知して警告を発行する。
    # 空リスト（`[]`）を指定すると検知を無効化できる。
    "textlint-protected-identifiers": [".NET", "Node.js", "Vue.js", "Next.js", "Nuxt.js"],
    "eslint": False,
    # pathが空文字の場合は{command}-runner設定（既定 "js-runner"）に基づいて自動解決する。
    "eslint-path": "",
    "eslint-runner": "js-runner",
    # ESLint 9系以降でcompact / unix / tapなどのコアフォーマッタが除去されたため、
    # 構造化出力はeslint-json設定により_STRUCTURED_OUTPUT_SPECS経由で注入する。
    "eslint-args": [],
    "eslint-fast": False,
    # fixモード時に通常argsの後に追加する引数。eslintは--fixでautofixする。
    "eslint-fix-args": ["--fix"],
    "prettier": False,
    "prettier-path": "",
    "prettier-runner": "js-runner",
    "prettier-args": [],
    # prettierは--check（read-only）と--write（書き込み）が排他のため、
    # pyfltrは2段階で実行する。詳細はcommand.pyの`_execute_prettier_two_step`を参照。
    "prettier-check-args": ["--check"],
    "prettier-write-args": ["--write"],
    "prettier-fast": True,
    "uv-sort": False,
    "uv-sort-path": "uv-sort",
    "uv-sort-args": [],
    "uv-sort-runner": "direct",
    "uv-sort-fast": True,
    "biome": False,
    "biome-path": "",
    "biome-runner": "js-runner",
    # "check"サブコマンドは共通argsに置く。--reporter=githubはbiome-json設定
    # により_STRUCTURED_OUTPUT_SPECS経由で注入する。
    "biome-args": ["check"],
    "biome-fast": True,
    # fixモード時に通常argsの後に追加する引数。
    # `biome check --write`でsafe fixのみ適用する（--unsafeは含めない）。
    "biome-fix-args": ["--write"],
    # -- js-runner対応ツール（追加分） --
    "oxlint": False,
    "oxlint-path": "",
    "oxlint-runner": "js-runner",
    "oxlint-args": [],
    "oxlint-fast": True,
    "tsc": False,
    "tsc-path": "",
    "tsc-runner": "js-runner",
    "tsc-args": ["--noEmit"],
    "tsc-pass-filenames": False,
    "tsc-fast": False,
    # -- Rust 言語ツール --
    # いずれも pass-filenames=False で crate 全体を対象とする project-level 実行。
    # 既定で bin-runner 経路を通り、グローバル `bin-runner` 既定 (mise) により mise exec で
    # 解決する。従来挙動 (PATH 上の cargo / cargo-deny を直接実行) を維持したい場合は
    # `cargo-fmt-runner = "direct"` 等の明示指定または `cargo-fmt-path` への明示パス指定で戻せる。
    "cargo-fmt": False,
    "cargo-fmt-path": "",
    "cargo-fmt-runner": "bin-runner",
    "cargo-fmt-version": "latest",
    # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
    "cargo-fmt-args": ["fmt"],
    "cargo-fmt-pass-filenames": False,
    "cargo-fmt-fast": True,
    "cargo-clippy": False,
    "cargo-clippy-path": "",
    "cargo-clippy-runner": "bin-runner",
    "cargo-clippy-version": "latest",
    # args は lint / fix 両モードで共通の前半部分。trailing flag (-- -D warnings)
    # は lint-args / fix-args の双方に重複して置き、--fix 時には `--fix` を
    # 中間に挿入できるよう分離している。
    "cargo-clippy-args": ["clippy", "--all-targets"],
    "cargo-clippy-lint-args": ["--", "-D", "warnings"],
    "cargo-clippy-fix-args": ["--fix", "--allow-staged", "--allow-dirty", "--", "-D", "warnings"],
    "cargo-clippy-pass-filenames": False,
    "cargo-clippy-fast": True,
    "cargo-check": False,
    "cargo-check-path": "",
    "cargo-check-runner": "bin-runner",
    "cargo-check-version": "latest",
    "cargo-check-args": ["check", "--all-targets"],
    "cargo-check-pass-filenames": False,
    "cargo-check-fast": False,
    "cargo-test": False,
    "cargo-test-path": "",
    "cargo-test-runner": "bin-runner",
    "cargo-test-version": "latest",
    "cargo-test-args": ["test"],
    "cargo-test-pass-filenames": False,
    "cargo-test-fast": False,
    "cargo-deny": False,
    "cargo-deny-path": "",
    "cargo-deny-runner": "bin-runner",
    "cargo-deny-version": "latest",
    "cargo-deny-args": ["check"],
    "cargo-deny-pass-filenames": False,
    "cargo-deny-fast": False,
    # -- .NET 言語ツール --
    # 既定で bin-runner 経路を通り、グローバル `bin-runner` 既定 (mise) により mise exec で
    # 解決する。従来挙動 (PATH 上の dotnet を直接実行) を維持したい場合は
    # `dotnet-format-runner = "direct"` 等の明示指定または `dotnet-format-path` への
    # 明示パス指定で戻せる。directモードでは`DOTNET_ROOT`環境変数配下にdotnet実行ファイルが
    # あれば優先採用する。
    "dotnet-format": False,
    "dotnet-format-path": "",
    "dotnet-format-runner": "bin-runner",
    "dotnet-format-version": "latest",
    # 常時書き込みモード。pyfltr 規約により formatter は --fix 無しでも強制修正する。
    "dotnet-format-args": ["format"],
    "dotnet-format-pass-filenames": False,
    "dotnet-format-fast": True,
    "dotnet-build": False,
    "dotnet-build-path": "",
    "dotnet-build-runner": "bin-runner",
    "dotnet-build-version": "latest",
    "dotnet-build-args": ["build", "--nologo"],
    "dotnet-build-pass-filenames": False,
    "dotnet-build-fast": False,
    "dotnet-test": False,
    "dotnet-test-path": "",
    "dotnet-test-runner": "bin-runner",
    "dotnet-test-version": "latest",
    "dotnet-test-args": ["test", "--nologo"],
    "dotnet-test-pass-filenames": False,
    "dotnet-test-fast": False,
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
    "ec": False,
    "ec-path": "",
    "ec-runner": "bin-runner",
    "ec-args": ["-format", "gcc", "-no-color"],
    "ec-version": "latest",
    "ec-fast": True,
    "shellcheck": False,
    "shellcheck-path": "",
    "shellcheck-runner": "bin-runner",
    "shellcheck-args": ["-f", "gcc"],
    "shellcheck-version": "latest",
    "shellcheck-fast": True,
    "typos": False,
    "typos-path": "typos",
    "typos-runner": "direct",
    "typos-args": ["--format", "brief"],
    "typos-version": "latest",
    "typos-fast": True,
    "actionlint": False,
    "actionlint-path": "",
    "actionlint-runner": "bin-runner",
    "actionlint-args": [],
    "actionlint-version": "latest",
    "actionlint-fast": True,
    # glab ci lint は GitLab API 経由で .gitlab-ci.yml を検証する。
    # 認証・ネットワーク必須のため既定で無効 (opt-in)。
    # サブコマンド `ci lint` は args 既定値として持たせ、明示 path 指定経路でも効くようにする。
    "glab-ci-lint": False,
    "glab-ci-lint-path": "",
    "glab-ci-lint-runner": "bin-runner",
    "glab-ci-lint-args": ["ci", "lint"],
    "glab-ci-lint-version": "latest",
    "glab-ci-lint-fast": False,
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
    # yamllint: Python製YAMLリンター。既定で無効（opt-in）。直接実行経路。
    "yamllint": False,
    "yamllint-path": "yamllint",
    "yamllint-runner": "direct",
    "yamllint-args": [],
    "yamllint-fast": True,
    # hadolint: Dockerfile専用リンター。bin-runner経由。既定で無効（opt-in）。
    "hadolint": False,
    "hadolint-path": "",
    "hadolint-runner": "bin-runner",
    "hadolint-args": [],
    "hadolint-version": "latest",
    "hadolint-fast": True,
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
    "pytest": False,
    "pytest-path": "pytest",
    "pytest-runner": "direct",
    "pytest-args": [],
    "pytest-devmode": True,  # PYTHONDEVMODE=1をするか否か
    "pytest-fast": False,
    "vitest": False,
    "vitest-path": "",
    "vitest-runner": "js-runner",
    # vitestはrunサブコマンドが必須。また、pyfltrがtargets設定で絞ったファイル群と
    # プロジェクト側のvitest include設定が交差せず対象ゼロになるケースでrc=1となり
    # failed扱いになるのを避けるため、--passWithNoTestsを既定に含める。
    "vitest-args": ["run", "--passWithNoTests"],
    "vitest-fast": False,
    "ruff-format": False,
    "ruff-format-path": "ruff",
    "ruff-format-runner": "direct",
    "ruff-format-args": ["format", "--exit-non-zero-on-format"],
    "ruff-format-fast": True,
    # ruff-format実行時にruff check --fix --unsafe-fixesを先に実行するか。
    # 既定では有効とし、未整形のimportソートや安全に自動修正できるlint違反を
    # フォーマットと一緒に片付ける（ruff公式推奨ワークフローの発展形）。
    # lintエラーは別途ruff-checkで検出される前提のため、ステップ1の
    # lint violation（exit 1）はruff-format側では失敗扱いしない。
    "ruff-format-by-check": True,
    "ruff-format-check-args": ["check", "--fix", "--unsafe-fixes"],
    "ruff-check": False,
    "ruff-check-path": "ruff",
    "ruff-check-runner": "direct",
    "ruff-check-args": ["check"],
    "ruff-check-fast": True,
    # fixモード時に通常argsの後に追加する引数。
    # `ruff check --fix --unsafe-fixes`でautofix可能な違反を修正する。
    # （通常モードのruff-format-by-checkとは別経路で動作する）
    "ruff-check-fix-args": ["--fix", "--unsafe-fixes"],
    # 実行アーカイブ（v3.0.0追加）
    # 全実行のツール生出力・diagnostic全件・実行メタをユーザーキャッシュ
    # （`platformdirs.user_cache_dir("pyfltr")`）へ保存する。CLIとは独立した
    # 詳細参照経路（`show-run` / `list-runs`、MCPツール）からいつでも
    # 全文を参照できるようにする。
    # 既定で有効にしている（オプトイン化を却下した）理由: エージェント連携時の
    # JSONL smart truncationで削られた情報を事後参照できる前提を崩さないため。
    # 肥大化は`archive-max-*`系の自動削除で抑える。
    "archive": True,
    # 自動クリーンアップの閾値。いずれかを超過した時点で古い順に削除する。
    # 0以下を指定するとその軸の自動削除は無効化される。
    "archive-max-runs": 100,
    "archive-max-size-mb": 1024,
    "archive-max-age-days": 30,
    # JSONL出力のsmart truncation設定（v3.0.0追加）。
    # `jsonl-diagnostic-limit`はツール単位のdiagnostic出力件数上限。0以下で無制限。
    # `jsonl-message-max-lines` / `jsonl-message-max-chars`はfailedかつdiagnostics=0のときの
    # tool.message（生出力末尾）を切り詰める閾値。
    # 切り詰めが発生しても、アーカイブ書き込みに成功していれば全文は`tools/<tool>/output.log`
    # / `tools/<tool>/diagnostics.jsonl`から復元できる。アーカイブ無効時 / 初期化失敗時 /
    # 当該ツールの書き込み失敗時は切り詰めをスキップしJSONLに全文を出力する。
    "jsonl-diagnostic-limit": 0,
    "jsonl-message-max-lines": 30,
    "jsonl-message-max-chars": 2000,
    # ファイルhashキャッシュ（v3.0.0 パートD）。
    # `CommandInfo.cacheable=True`のツール（textlint）の実行結果をユーザーキャッシュへ保存し、
    # 同じ入力（対象ファイル群・設定ファイル・実効コマンドライン等）が繰り返された場合に
    # ツール実行を省略して結果を復元する。エージェントが同じmarkdownに対してtextlintを
    # 繰り返し呼び出すワークフローでの待機時間を削減する用途。
    # `--no-cache`CLIフラグまたは`cache = false`設定で無効化できる。
    # `cache-max-age-hours`は保存期間（時間）で、短期破棄前提として既定12時間。
    # 0以下で期間軸のクリーンアップを無効化する。
    "cache": True,
    "cache-max-age-hours": 12,
    # 最大並列数（linters/testersの並列実行数の上限）
    "jobs": 4,
    # flake8風無視パターン。
    "exclude": [
        # ここの値はflake8やblackなどの既定値を元に適当に。
        # https://github.com/github/gitignore/blob/master/Python.gitignore
        # https://github.com/github/gitignore/blob/main/Node.gitignore
        "*.egg",
        "*.egg-info",
        ".aider*",
        ".bzr",
        ".cache",
        ".cursor",
        ".direnv",
        ".eggs",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".nox",
        ".pnpm",
        ".pyre",
        ".pytest_cache",
        ".ruff_cache",
        ".serena",
        ".svn",
        ".tox",
        ".venv",
        ".vite",
        ".vscode",
        ".yarn",
        "CVS",
        "__pycache__",
        "__pypackages__",
        "_build",
        "buck-out",
        "build",
        "dist",
        "node_modules",
        "site",
        "venv",
        # バイナリファイル（テキスト系lintの対象外）
        "*.bmp",
        "*.dll",
        "*.dylib",
        "*.eot",
        "*.exe",
        "*.gif",
        "*.gz",
        "*.ico",
        "*.jpeg",
        "*.jpg",
        "*.mp3",
        "*.mp4",
        "*.otf",
        "*.pdf",
        "*.png",
        "*.so",
        "*.tar",
        "*.ttf",
        "*.wasm",
        "*.wav",
        "*.webp",
        "*.woff",
        "*.woff2",
        "*.zip",
    ],
    "extend-exclude": [],
    # .gitignoreに記載されたファイルを除外するか否か（git check-ignoreを使用）
    "respect-gitignore": True,
    # コマンド名のエイリアス
    "aliases": {
        "format": [
            "pre-commit",
            "ruff-format",
            "prettier",
            "uv-sort",
            "shfmt",
            "taplo",
            "cargo-fmt",
            "dotnet-format",
        ],
        "lint": [
            "ruff-check",
            "mypy",
            "pylint",
            "pyright",
            "ty",
            "markdownlint",
            "textlint",
            "eslint",
            "biome",
            "ec",
            "shellcheck",
            "typos",
            "actionlint",
            "glab-ci-lint",
            "yamllint",
            "hadolint",
            "gitleaks",
            "oxlint",
            "tsc",
            "cargo-clippy",
            "cargo-check",
            "cargo-deny",
            "dotnet-build",
        ],
        "test": ["pytest", "vitest", "cargo-test", "dotnet-test"],
    },
}
"""デフォルト設定。"""


@dataclasses.dataclass(frozen=True)
class Config:
    """pyfltr設定。"""

    values: dict[str, typing.Any]
    commands: dict[str, CommandInfo]
    """ビルトイン + カスタムの統合コマンドレジストリ"""
    command_names: list[str]
    """コマンドの並び順リスト（ビルトイン順 → カスタムコマンド順）"""

    def __getitem__(self, key: str) -> typing.Any:
        """設定値を取得。"""
        return self.values[key]


def create_default_config() -> Config:
    """デフォルト設定を生成。"""
    config = Config(
        values=copy.deepcopy(DEFAULT_CONFIG),
        commands=dict(BUILTIN_COMMANDS),
        command_names=list(BUILTIN_COMMAND_NAMES),
    )
    config.values["aliases"]["fast"] = _build_fast_alias(config)
    return config


def load_config(config_dir: pathlib.Path | None = None) -> Config:
    """pyproject.tomlから設定を読み込む。"""
    config = create_default_config()
    base = config_dir or pathlib.Path.cwd()
    pyproject_path = (base / "pyproject.toml").absolute()
    if not pyproject_path.exists():
        return config

    with pyproject_path.open("rb") as f:
        pyproject_data = tomllib.load(f)

    tool_pyfltr = pyproject_data.get("tool", {}).get("pyfltr", {})

    # プリセットの反映
    preset = str(tool_pyfltr.get("preset", ""))
    if preset == "":
        pass
    elif preset in _PRESETS:
        config.values.update(_PRESETS[preset])
    elif preset in _REMOVED_PRESETS:
        raise ValueError(_REMOVED_PRESETS[preset])
    else:
        raise ValueError(f"preset の設定値が正しくありません。{preset=}")

    # カスタムコマンドの読み込み
    custom_commands = tool_pyfltr.get("custom-commands", tool_pyfltr.get("custom_commands", {}))
    if not isinstance(custom_commands, dict):
        raise ValueError("custom-commandsはテーブルで指定してください")
    for name, definition in custom_commands.items():
        name = name.replace("_", "-")
        _register_custom_command(config, name, definition)

    # 言語カテゴリgateの適用（preset < 言語カテゴリgate < 個別設定）
    # v3.0.0でpython / javascript / rust / dotnetを同じ枠組みのカテゴリキーに統一した。
    # presetは各時点の推奨構成として全言語のツールを横断的にTrueにするが、カテゴリ
    # キーがFalse（既定）のときはpreset由来のTrueをFalseへ押し戻して実行を抑止する。
    # 後続の個別設定ループで`{command} = true` / `{command} = false`による上書きが可能
    # （個別指定はgateを越えて最優先）。
    # 設定キーの「_」「-」ゆらぎに対応するため、ユーザー入力側のキー集合を正規化しておく。
    user_keys = {key.replace("_", "-") for key in tool_pyfltr}
    for category_key, commands in LANGUAGE_CATEGORIES:
        if bool(tool_pyfltr.get(category_key, False)):
            continue  # gate 開放: preset 由来の True をそのまま通す
        for cmd in commands:
            if cmd in user_keys:
                continue  # 個別設定による明示指定を保持 (True/False 双方)
            config.values[cmd] = False

    # プリセット・言語カテゴリ以外の設定を適用（プリセットと重複があれば上書き）
    skip_keys = ("custom-commands", *(key for key, _ in LANGUAGE_CATEGORIES))
    targets_overrides: dict[str, str | list[str]] = {}
    extend_targets_map: dict[str, str | list[str]] = {}
    for key, value in tool_pyfltr.items():
        key = key.replace("_", "-")  # 「_」区切りと「-」区切りのどちらもOK
        if key in skip_keys:
            continue  # 別途処理済み
        # v3.0.0で削除されたツール名に紐づく設定キーを検出したら移行案内を出す。
        # "pyupgrade" / "pyupgrade-path" / "pyupgrade-args" / "pyupgrade-fast"などを網羅する。
        removed_owner = _extract_removed_command(key)
        if removed_owner is not None:
            raise ValueError(
                f'"{key}" は v3.0.0 で削除されたツール "{removed_owner}" 向けの設定である。'
                "5 ツール (pyupgrade / autoflake / isort / black / pflake8) は ruff への統合により削除された。"
                "該当設定をすべて pyproject.toml から除去すること"
            )
        # {command}-excludeの検出
        if key.endswith("-exclude"):
            cmd_name = key.removesuffix("-exclude")
            if cmd_name in config.commands:
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    raise ValueError(f"設定値が不正です: {key} はstr型のリストで指定してください")
                config.values[key] = value
                continue
        # {command}-extend-targetsの検出（長いサフィックスを先に判定）
        if key.endswith("-extend-targets"):
            cmd_name = key.removesuffix("-extend-targets")
            if cmd_name in config.commands:
                extend_targets_map[cmd_name] = _validate_targets_value(key, value)
                continue
        # {command}-targetsの検出
        if key.endswith("-targets"):
            cmd_name = key.removesuffix("-targets")
            if cmd_name in config.commands:
                targets_overrides[cmd_name] = _validate_targets_value(key, value)
                continue
        if key not in config.values:
            raise ValueError(f"設定キーが不正です: {key}")
        if not isinstance(value, type(config.values[key])):  # 簡易チェック
            raise ValueError(f"設定値が不正です: {key}={type(value)}, expected {type(config.values[key])}")
        config.values[key] = value

    # targetsの完全上書き
    for cmd_name, new_targets in targets_overrides.items():
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=new_targets)

    # extend-targetsの追加（targets上書き後に適用）
    for cmd_name, extra in extend_targets_map.items():
        existing = config.commands[cmd_name].target_globs()
        if isinstance(extra, str):
            existing.append(extra)
        else:
            existing.extend(extra)
        config.commands[cmd_name] = dataclasses.replace(config.commands[cmd_name], targets=existing)

    # typos-pathの互換正規化: generate-configが出力した空文字列を "typos" へ変換する
    if config.values["typos-path"] == "":
        config.values["typos-path"] = "typos"

    # js-runnerの値バリデーション
    js_runner = config.values["js-runner"]
    if js_runner not in JS_RUNNERS:
        raise ValueError(f"js-runnerの設定値が正しくありません。{js_runner=} (許容値: {', '.join(JS_RUNNERS)})")

    # bin-runnerの値バリデーション
    bin_runner = config.values["bin-runner"]
    if bin_runner not in BIN_RUNNERS:
        raise ValueError(f"bin-runnerの設定値が正しくありません。{bin_runner=} (許容値: {', '.join(BIN_RUNNERS)})")

    # {command}-runnerの値バリデーション
    # 各コマンドごとに`"direct"` / `"mise"` / `"bin-runner"` / `"js-runner"`の4値のみ許容する。
    for key, value in config.values.items():
        if not key.endswith("-runner") or key in ("bin-runner", "js-runner"):
            continue
        if value not in COMMAND_RUNNERS:
            raise ValueError(f"{key}の設定値が正しくありません。{value=!r} (許容値: {', '.join(COMMAND_RUNNERS)})")

    # per-command fastフラグからfastエイリアスを再計算
    config.values["aliases"]["fast"] = _build_fast_alias(config)

    # 有効化されているコマンドのconfig_filesが見つからなければ警告
    _warn_missing_config_files(config, base)

    return config


def _warn_missing_config_files(config: Config, base: pathlib.Path) -> None:
    """有効化されているコマンドで`CommandInfo.config_files`を満たさないものを警告する。"""
    # 遅延importで循環依存を避ける（warnings_はpyfltr内で広く参照されるため）
    import pyfltr.warnings_  # pylint: disable=import-outside-toplevel

    for command, info in config.commands.items():
        if not info.config_files:
            continue
        if config.values.get(command) is not True:
            continue
        if any(list(base.glob(pattern)) for pattern in info.config_files):
            continue
        candidates = ", ".join(info.config_files)
        pyfltr.warnings_.emit_warning(
            source="config",
            message=f"{command} が有効化されていますが、設定ファイルが見つかりません: {candidates}",
        )


def _register_custom_command(config: Config, name: str, definition: dict[str, typing.Any]) -> None:
    """カスタムコマンドをConfigに登録する。"""
    # 名前衝突チェック
    if name in BUILTIN_COMMANDS:
        raise ValueError(f"カスタムコマンド名がビルトインコマンドと衝突しています: {name}")

    # type (必須)
    cmd_type = definition.get("type")
    if cmd_type not in ("formatter", "linter", "tester"):
        raise ValueError(f"カスタムコマンド {name} のtypeが不正です: {cmd_type}")

    # path (省略時はコマンド名)
    path = definition.get("path", name)
    if not isinstance(path, str):
        raise ValueError(f"カスタムコマンド {name} のpathは文字列で指定してください")

    # args (省略時は空リスト)
    args = definition.get("args", [])
    if not isinstance(args, list):
        raise ValueError(f"カスタムコマンド {name} のargsはリストで指定してください")

    # fix-args（省略可。省略時はfixモード非対応として扱う）
    fix_args = definition.get("fix-args", definition.get("fix_args"))
    if fix_args is not None and not isinstance(fix_args, list):
        raise ValueError(f"カスタムコマンド {name} のfix-argsはリストで指定してください")

    # targets（省略時は "*.py"。strまたはlist[str]）
    raw_targets: typing.Any = definition.get("targets", "*.py")
    targets: str | list[str]
    if isinstance(raw_targets, str):
        targets = raw_targets
    elif isinstance(raw_targets, list) and all(isinstance(item, str) for item in raw_targets):
        # raw_targetsはtyping.Any経由のためlist(raw_targets)の要素型が縮まらない。
        # 上記isinstanceで要素がstrであることを検証済みなので、明示的にstr化して
        # list[str]を構築する。
        targets = [str(item) for item in raw_targets]
    else:
        raise ValueError(f"カスタムコマンド {name} のtargetsは文字列または文字列のリストで指定してください")

    # error-pattern（省略可）
    error_pattern = definition.get("error-pattern", definition.get("error_pattern"))
    if error_pattern is not None:
        if not isinstance(error_pattern, str):
            raise ValueError(f"カスタムコマンド {name} のerror-patternは文字列で指定してください")
        _validate_error_pattern(name, error_pattern)

    # config-files（省略可。設定ファイル候補のglobパターン）
    raw_config_files: typing.Any = definition.get("config-files", definition.get("config_files", []))
    if not isinstance(raw_config_files, list) or not all(isinstance(item, str) for item in raw_config_files):
        raise ValueError(f"カスタムコマンド {name} のconfig-filesは文字列のリストで指定してください")
    config_files: list[str] = [str(item) for item in raw_config_files]

    # CommandInfoを登録
    config.commands[name] = CommandInfo(
        type=cmd_type,
        builtin=False,
        targets=targets,
        error_pattern=error_pattern,
        config_files=config_files,
    )
    config.command_names.append(name)

    # fast（省略時はFalse）
    fast = definition.get("fast", False)
    if not isinstance(fast, bool):
        raise ValueError(f"カスタムコマンド {name} のfastはboolで指定してください")

    # pass-filenames（省略時はTrue）
    pass_filenames = definition.get("pass-filenames", definition.get("pass_filenames", True))
    if not isinstance(pass_filenames, bool):
        raise ValueError(f"カスタムコマンド {name} のpass-filenamesはboolで指定してください")

    # values辞書にデフォルト設定を追加
    config.values[name] = True
    config.values[f"{name}-path"] = path
    config.values[f"{name}-args"] = args
    config.values[f"{name}-fast"] = fast
    config.values[f"{name}-pass-filenames"] = pass_filenames
    # fix-argsは定義されている場合のみ登録する（キーの有無でfix対応可否を判別）
    if fix_args is not None:
        config.values[f"{name}-fix-args"] = fix_args


def _build_fast_alias(config: Config) -> list[str]:
    """per-command fastフラグからfastエイリアスを動的構築。"""
    return [name for name in config.command_names if config.values.get(f"{name}-fast", False)]


def _validate_error_pattern(name: str, pattern: str) -> None:
    """error-patternのバリデーション。"""
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"カスタムコマンド {name} のerror-patternが不正な正規表現です: {e}") from e
    # 必須グループの確認
    groups = compiled.groupindex
    for required in ("file", "line", "message"):
        if required not in groups:
            raise ValueError(f"カスタムコマンド {name} のerror-patternに{required}グループが必要です")


def _extract_removed_command(key: str) -> str | None:
    """設定キーが削除コマンド宛なら該当コマンド名を返す、そうでなければNone。

    `"pyupgrade"`のようなbare keyと、`"pyupgrade-path"` / `"pyupgrade-args"` /
    `"pyupgrade-fast"`などの派生キーの双方を検出する。
    """
    if key in REMOVED_COMMANDS:
        return key
    for command in REMOVED_COMMANDS:
        if key.startswith(f"{command}-"):
            return command
    return None


def _validate_targets_value(key: str, value: typing.Any) -> str | list[str]:
    """Targets / extend-targets の値をバリデーション。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [str(item) for item in value]
    raise ValueError(f"{key}は文字列または文字列のリストで指定してください")


def filter_fix_commands(commands: list[str], config: Config) -> list[str]:
    """fixステージで実行すべきコマンドに絞り込む。

    `pyfltr run` / `pyfltr fast`のfixステージはlinterのautofix機能
    （`{command}-fix-args`）を前段で呼び出すための段で、formatterは対象外。
    formatter本体は通常ステージで常に書き込みモードで動くため、fixステージで
    重複して走らせる必要はない。

    enabledかつ`{command}-fix-args`が定義されているlinter/testerを返す。
    """
    result: list[str] = []
    for command in commands:
        if not config[command]:
            continue
        if f"{command}-fix-args" in config.values:
            result.append(command)
    return result


def resolve_aliases(commands: list[str], config: Config) -> list[str]:
    """エイリアスを展開する。"""
    # 最大10回まで再帰的に展開
    result: list[str] = []
    for _ in range(10):
        result = []
        resolved: bool = False
        for command in commands:
            command = command.strip()
            if command in config["aliases"]:
                for c in config["aliases"][command]:
                    if c not in result:  # 順番は維持しつつ重複排除
                        result.append(c)
                resolved = True
            else:
                if command not in result:  # 順番は維持しつつ重複排除
                    result.append(command)
        if not resolved:
            break
        commands = result
    result.sort(key=config.command_names.index)  # リスト順にソート
    return result


def generate_config_text(config: Config | None = None) -> str:
    """設定ファイルのサンプルテキストを生成。"""
    if config is None:
        config = create_default_config()
    return "[tool.pyfltr]\n" + "\n".join(
        f"{key} = " + repr(value).replace("'", '"').replace("True", "true").replace("False", "false")
        for key, value in config.values.items()
    )
