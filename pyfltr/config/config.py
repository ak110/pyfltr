"""設定関連の処理。

TOMLの読み書きはコメント・セクション順を保持できる`tomlkit`に統一する
（`tomllib`は使用しない）。`pyfltr config set`等での部分編集で
ユーザーが手書きしたコメントを維持するために必要。
"""

# pylint: disable=too-many-lines

import copy
import dataclasses
import os
import pathlib
import re
import typing

import platformdirs
import tomlkit
import tomlkit.exceptions

import pyfltr.warnings_
from pyfltr.command.builtin import (
    BIN_RUNNERS,
    BUILTIN_COMMAND_NAMES,
    BUILTIN_COMMANDS,
    COMMAND_RUNNERS,
    JS_RUNNERS,
    LANGUAGE_CATEGORIES,
    PYTHON_RUNNERS,
    REMOVED_COMMANDS,
    CommandInfo,
)
from pyfltr.config.presets import _PRESETS, _REMOVED_PRESETS

# global優先キーのSSOT。
# archive/cache系の設定値はマシン単位で揃えたい性質のため、
# `~/.config/pyfltr/config.toml`（global設定）に書かれた値をproject側より優先する。
# 通常キーはproject優先（後勝ち）であるのに対して、本集合は逆向きの優先順を持つ。
# 範囲拡大時は `docs/guide/configuration.md` と関連テストも併せて更新する（人手同期）。
ARCHIVE_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "archive",
        "archive-max-runs",
        "archive-max-size-mb",
        "archive-max-age-days",
    }
)
CACHE_CONFIG_KEYS: frozenset[str] = frozenset({"cache", "cache-max-age-hours"})
GLOBAL_PRIORITY_KEYS: frozenset[str] = ARCHIVE_CONFIG_KEYS | CACHE_CONFIG_KEYS

SEVERITY_VALUES: tuple[str, ...] = ("error", "warning")
"""`{command}-severity`に指定可能な値。

- `"error"`（既定）: 従来通り。失敗時にJSONL `status="failed"` を返し、パイプライン全体のexit codeも非0となる
- `"warning"`: 失敗時にJSONL `status="warning"` を返す。`commands_summary.needs_action.warning` に集計するが、
  `failure_present` 判定からは除外するため `summary.guidance` のfailure系は出力されず、パイプラインのexit codeにも影響しない
"""

EXPAND_USER_KEY_SUFFIXES: tuple[str, ...] = ("-path", "-args", "-fix-args")
"""`~`展開を適用する設定キーのサフィックス集合。

利用者ホームディレクトリ依存のパス（例: `~/dotfiles/.../tool.py`）を設定値として
記述できるようにするため、特定のper-toolキーに限り `~` 展開を適用する。
対象は `{command}-path` / `{command}-args` / `{command}-lint-args` / `{command}-fix-args` /
`{command}-check-args` / `{command}-write-args` および `ruff-format-check-args`。

`config-files` / `targets` / `{command}-extend-targets` 等のglobパターン用キーは
glob内チルダの意図しない展開を防ぐため対象外とする。

展開規則は要素先頭の `~` に加え、要素内の最初の `=` 直後の `~` も展開する
（`--config=~/cfg.toml` を `--config=<HOME>/cfg.toml` に展開）。
`os.path.expanduser` は先頭の `~` のみ展開するため、展開は
`pyfltr.command.runner.expanduser_args` を経由する。

展開タイミングはsubprocess引数組み立て直前（`pyfltr.command.runner.build_commandline` /
`build_invocation_argv` および `pyfltr.command.two_step.base` の各経路）で、
`config.values` 読込時点では原文を保持する（`command-info` サブコマンドの
`configured_path` / `configured_args` にも原文が露出する）。
"""


DEFAULT_CONFIG: dict[str, typing.Any] = {
    # キー体系の方針:
    # - per-toolキーは `{command}-{key}` 形式（例: `ruff-check-args`・`pylint-runner`）。
    # - `{command}-runner` の既定値は対応するカテゴリ委譲値で揃える。
    #   Python系（mypy・pylint・pyright・ruff-* 等）は `"python-runner"` 委譲、
    #   JS系（textlint・eslint・biome 等）は `"js-runner"` 委譲、
    #   ネイティブ系（shellcheck・shfmt・cargo-* 等）は `"bin-runner"` 委譲。
    #   これによりグローバル `python-runner` / `js-runner` / `bin-runner` の切り替え1箇所で
    #   ツール群の起動経路を一括変更できる。
    # - グローバル既定値は `python-runner = "uv"`・`js-runner = "pnpx"`・`bin-runner = "mise"`。
    # - 利用者は `{command}-runner = "direct"` または `{command}-path` の明示で個別に上書きできる。
    # - per-tool直接指定値の許容範囲とカテゴリ委譲値の対応詳細は
    #   `pyfltr.command.runner.build_commandline` のdocstringを参照する。
    # プリセット
    "preset": "",
    # 言語カテゴリキー: presetが示す言語別ツールを通過させるgateとして働く。
    # Trueならpreset由来で有効化された該当カテゴリのコマンドをそのまま通し、
    # False（既定）ならpresetでTrueになった該当コマンドを個別指定がない限り
    # Falseに上書きする。カテゴリキー単独では何も有効化されない（presetか個別
    # `{command} = true`が必要）。v3.0.0で既定値をFalse（opt-in）に統一。
    # 対象外プロジェクトで言語別linterが自動で実行されるのを防ぐためで、Python系は
    # Python系ツール一式が本体依存に同梱済みのため `uvx pyfltr` で揃う。
    # JavaScript / Rust / .NET系はそれぞれのツールチェインを前提とする。
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
    # Python系ツール（mypy / pylint / pyright / ty / pytest / ruff-format / ruff-check / uv-sort）の
    # 起動方式。{command}-path / {command}-runner明示が無いときに、以下の値に従って起動コマンドを組み立てる。
    # - direct: shutil.whichで本体依存に同梱されたバイナリを直接起動
    # - uv:     cwdにuv.lockがあり、かつuvが利用可能ならuv run --frozen <bin>経由で起動。
    #           いずれかが満たされなければdirectへフォールバック（既定。従来互換）
    # - uvx:    uvx <bin>形式でPyPI最新版を都度取得して起動（uv.lockは参照せず、{command}-versionとも連動しない）
    "python-runner": "uv",
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
    # pathが空文字の場合は{command}-runner設定
    # （既定はツール群に応じてpython-runner/js-runner/bin-runner）に基づいて自動解決する。
    # python-runner経路の既定（"uv"）によりcwdのuv.lock検出時はプロジェクトのuv環境を使う。
    # {command}-path明示で従来挙動（指定パスを直接実行）に切り替えられる。
    "mypy-path": "",
    "mypy-args": [],
    "mypy-runner": "python-runner",
    "mypy-fast": False,
    "pylint": False,
    "pylint-path": "",
    "pylint-args": [],
    "pylint-runner": "python-runner",
    "pylint-fast": False,
    "pyright": False,
    "pyright-path": "",
    "pyright-args": [],
    "pyright-runner": "python-runner",
    "pyright-fast": False,
    "ty": False,
    "ty-path": "",
    "ty-args": ["check", "--output-format", "concise", "--error-on-warning"],
    "ty-runner": "python-runner",
    "ty-fast": True,
    "markdownlint": False,
    # ユーザーが明示的にpathを設定した場合はその値をそのまま使い、args先頭に自動prefixを追加しない。
    "markdownlint-path": "",
    "markdownlint-args": [],
    "markdownlint-runner": "js-runner",
    "markdownlint-fast": True,
    # fixステージ（pyfltr run / fastの自動修正段）で通常argsの後に追加する引数。
    # markdownlint-cli2は--fixでファイルをin-place修正する。
    "markdownlint-fix-args": ["--fix"],
    "textlint": False,
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
    # pyfltrは2段階で実行する。詳細はcommand.pyの`execute_prettier_two_step`を参照。
    "prettier-check-args": ["--check"],
    "prettier-write-args": ["--write"],
    "prettier-fast": True,
    "uv-sort": False,
    "uv-sort-path": "",
    "uv-sort-args": [],
    "uv-sort-runner": "python-runner",
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
    # `cargo-fmt-runner = "direct"` 等の明示指定または `cargo-fmt-path` への明示パス指定で切り替えられる。
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
    "typos-path": "",
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
    # サブコマンド `ci lint` は args 既定値として持たせ、明示 path 指定経路でも適用されるようにする。
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
    "yamllint-path": "",
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
    "pytest-path": "",
    "pytest-runner": "python-runner",
    "pytest-args": [],
    "pytest-devmode": True,  # PYTHONDEVMODE=1をするか否か
    "pytest-fast": False,
    "vitest": False,
    "vitest-path": "",
    "vitest-runner": "js-runner",
    # vitestはrunサブコマンドが必須。また、pyfltrがtargets設定で限定したファイル群と
    # プロジェクト側のvitest include設定が交差せず対象ゼロになるケースでrc=1となり
    # failed扱いになるのを避けるため、--passWithNoTestsを既定に含める。
    "vitest-args": ["run", "--passWithNoTests"],
    "vitest-fast": False,
    "ruff-format": False,
    "ruff-format-path": "",
    "ruff-format-runner": "python-runner",
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
    "ruff-check-path": "",
    "ruff-check-runner": "python-runner",
    "ruff-check-args": ["check"],
    "ruff-check-fast": True,
    # fixモード時に通常argsの後に追加する引数。
    # `ruff check --fix --unsafe-fixes`でautofix可能な違反を修正する。
    # （通常モードのruff-format-by-checkとは別経路で動作する）
    "ruff-check-fix-args": ["--fix", "--unsafe-fixes"],
    # 実行アーカイブ（v3.0.0追加）
    # 全実行のツール生出力・diagnostic全件・実行メタをユーザーキャッシュ
    # （`platformdirs.user_cache_dir("pyfltr", appauthor=False)`）へ保存する。CLIとは独立した
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
    # replace履歴の自動クリーンアップ閾値（pyfltr/grep_/history.py）。
    # 既定値は実行アーカイブと同程度に揃え、世代数100・合計200MB・保存期間30日とする。
    # `GLOBAL_PRIORITY_KEYS`には含めず、project側設定で上書きできる通常キー扱いとする。
    "replace-history-max-entries": 100,
    "replace-history-max-size-bytes": 200 * 1024 * 1024,
    "replace-history-max-age-days": 30,
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
    # 各コマンドのsubprocess実行に対する壁時計タイムアウト（秒）。
    # 既定値10分（600秒）。0以下を指定すると無効化される（無制限）。
    # per-tool `{command}-timeout` が `-1`（既定。「未設定」を意味するsentinel）のとき
    # 本グローバル値を採用し、`0` 以上の値が明示された場合はそちらを優先する。
    # ハング由来の停止はJSONL `command.hints` の `status.timeout` 注記で識別できる。
    "command-timeout": 600,
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
            "prettier",
            "ruff-format",
            "uv-sort",
            "shfmt",
            "taplo",
            "cargo-fmt",
            "dotnet-format",
            "pre-commit",
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


# per-tool `{command}-timeout` キーをビルトインコマンドぶん追加する。
# 既定値 `-1` は「未設定」を意味するsentinelで、解決時にグローバル `command-timeout` 値へフォールバックする。
# `0` 以下を明示指定した場合は当該コマンドのtimeoutを無効化する。
# `>0` の場合は秒数を指定する。`per-tool` 値が `-1` 以外なら本値を優先する。
# 命名は既存の `{command}-args` / `{command}-fast` 系と同パターンに揃える。
# 別のper-toolキーで「未指定でグローバル値へフォールバック」を表現する場合も同じ `-1`
# sentinel運用に揃える。`None` 表現はTOML上の素直な記述方法が無く、整数フィールド
# としての一貫性も崩れるため採用しない。
# モジュールトップレベルでの `for` ループ変数のスコープ漏れを避けるため関数経由で適用する
# （pyrightの `reportPossiblyUnboundVariable` 誤検知も同時に回避）。
def _register_command_timeout_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    """全ビルトインコマンドへ `{command}-timeout = -1` のsentinel既定値を登録する。"""
    for command in command_names:
        defaults[f"{command}-timeout"] = -1


_register_command_timeout_defaults(DEFAULT_CONFIG, BUILTIN_COMMAND_NAMES)


def _register_command_severity_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    """全ビルトインコマンドへ `{command}-severity = "error"` の既定値を登録する。

    `severity = "warning"` 設定下では従来 `failed` 扱いの結果がJSONL上 `status="warning"` に切り替わる。
    既定値 `"error"` は従来挙動を維持するためのもので、変更したい場合のみ
    `pyproject.toml`/global設定で個別に指定する。
    """
    for command in command_names:
        defaults[f"{command}-severity"] = "error"


def _register_command_hints_defaults(defaults: dict[str, typing.Any], command_names: list[str]) -> None:
    """全ビルトインコマンドへ `{command}-hints = []` の既定値を登録する。

    指摘1件以上のときに限りJSONL `command.hints` の `user.<n>` キーへ
    順番に追加される。既定の空配列はLLM入力にhintを追加しない挙動を意味する。
    """
    for command in command_names:
        defaults[f"{command}-hints"] = []


_register_command_severity_defaults(DEFAULT_CONFIG, BUILTIN_COMMAND_NAMES)
_register_command_hints_defaults(DEFAULT_CONFIG, BUILTIN_COMMAND_NAMES)


def resolve_severity(values: dict[str, typing.Any], command: str) -> str:
    """per-tool `{command}-severity` の有効値を返す。

    既定値 `"error"` は従来挙動と同じ。`"warning"` 設定時は
    `CommandResult.severity` フィールドへ転記され、`status` プロパティが
    通常失敗を `"warning"` に置き換える。未知の値は `"error"` として扱う
    （バリデーションは `load_config` 側で行う）。
    """
    raw = values.get(f"{command}-severity", "error")
    if raw in SEVERITY_VALUES:
        return str(raw)
    return "error"


def resolve_command_timeout(values: dict[str, typing.Any], command: str) -> float | None:
    """per-tool `{command}-timeout` とグローバル `command-timeout` から有効値を解決する。

    `{command}-timeout`の意味は次の通り。

    - `-1`（既定sentinel）または負値: 「未設定」を意味し、グローバル `command-timeout`
      の値へフォールバックする。利用者向けドキュメントでは「未指定」と表現する
    - `0`: 当該per-toolのtimeoutを明示的に無効化する（戻り値`None`）
    - 正の整数: 当該秒数で監視する

    グローバル`command-timeout`は次の通り。

    - `0`: 全コマンドのtimeoutを無効化する
    - 正の整数: per-tool未設定時の既定秒数として採用される

    `None` を返した場合 `pyfltr.command.process.run_subprocess` はtimeout監視を行わない。
    `float` を返した場合は当該秒数で監視する。
    """
    per_tool_raw = values.get(f"{command}-timeout", -1)
    try:
        per_tool = int(per_tool_raw)
    except (TypeError, ValueError):
        per_tool = -1
    if per_tool >= 0:
        return float(per_tool) if per_tool > 0 else None
    # per-tool未設定（sentinel）→グローバル値へフォールバック
    global_raw = values.get("command-timeout", 0)
    try:
        global_value = int(global_raw)
    except (TypeError, ValueError):
        global_value = 0
    return float(global_value) if global_value > 0 else None


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


def default_global_config_path() -> pathlib.Path:
    r"""XDG準拠のグローバル設定ファイルパスを返す。

    Linuxでは`~/.config/pyfltr/config.toml`、macOSでは
    `~/Library/Application Support/pyfltr/config.toml`、
    Windowsでは`%LOCALAPPDATA%\pyfltr\config.toml`になる。
    環境変数`PYFLTR_GLOBAL_CONFIG`が設定されていればそれを優先する
    （テスト容易性確保とユーザーの強制上書き用。`PYFLTR_CACHE_DIR`と命名対称）。

    `appauthor=False`を渡すのは、未指定時にWindowsで`appname`が
    appauthorとしても付与され`%LOCALAPPDATA%\pyfltr\pyfltr\config.toml`に
    なる挙動を回避するため。
    """
    override = os.environ.get("PYFLTR_GLOBAL_CONFIG")
    if override:
        return pathlib.Path(override)
    return pathlib.Path(platformdirs.user_config_dir("pyfltr", appauthor=False)) / "config.toml"


def _read_global_config(path: pathlib.Path) -> dict[str, typing.Any]:
    """globalのconfig.tomlを読み込み、`[tool.pyfltr]`配下を返す。

    ファイル不在時は空辞書を返す。TOML構文エラー時は`ValueError`で停止する。
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as e:
        raise ValueError(f"global設定ファイルのTOMLが不正です: {path}: {e}") from e
    raw = data.get("tool", {})
    raw = raw.get("pyfltr", {}) if isinstance(raw, dict) else {}
    return _unwrap_tomlkit(raw) if isinstance(raw, dict) else {}


def _unwrap_tomlkit(value: typing.Any) -> typing.Any:
    """tomlkitの値を素のPython値（dict / list / 基本型）へ再帰的に変換する。

    tomlkitはInteger / String / Bool等のラッパー型で値を返す。
    `isinstance(v, int)`等の判定は通常通り動くが、`config.values`に格納すると
    JSON serializeや`==`比較で予期せぬ挙動になる場合があるため、
    入力段で純粋なPython値へ揃えておく。
    """
    if isinstance(value, dict):
        return {str(k): _unwrap_tomlkit(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_tomlkit(item) for item in value]
    unwrap = getattr(value, "unwrap", None)
    if callable(unwrap):
        return unwrap()
    return value


def _merge_global_and_project(
    global_data: dict[str, typing.Any],
    project_data: dict[str, typing.Any],
) -> tuple[dict[str, typing.Any], dict[str, set[str]]]:
    """Global / project由来の`[tool.pyfltr]`辞書をマージし、由来情報を返す。

    通常キーはproject優先（globalの値はprojectで上書きされる）。
    `GLOBAL_PRIORITY_KEYS`に含まれるarchive/cache系はglobal優先で、
    両方にある場合はglobal値で上書きする。
    キー名の`_`は`-`に正規化してから判定する
    （同じキーが`archive_max_runs`と`archive-max-runs`で別計上されないように）。

    Returns:
        マージ済みdict（normalized key → value）と、
        normalized keyから`{"global", "project"}`部分集合への辞書のタプル。
    """
    normalized_global = {key.replace("_", "-"): value for key, value in global_data.items()}
    normalized_project = {key.replace("_", "-"): value for key, value in project_data.items()}

    key_sources: dict[str, set[str]] = {}
    for key in normalized_global:
        key_sources.setdefault(key, set()).add("global")
    for key in normalized_project:
        key_sources.setdefault(key, set()).add("project")

    merged: dict[str, typing.Any] = {}
    for key, sources in key_sources.items():
        if key in GLOBAL_PRIORITY_KEYS:
            # archive / cache系: globalがあればglobal、無ければproject。
            if "global" in sources:
                merged[key] = normalized_global[key]
            else:
                merged[key] = normalized_project[key]
        else:
            # 通常キー: 後勝ち（project優先）。
            if "project" in sources:
                merged[key] = normalized_project[key]
            else:
                merged[key] = normalized_global[key]
    return merged, key_sources


def load_config(
    config_dir: pathlib.Path | None = None,
    *,
    global_config_path: pathlib.Path | None = None,
) -> Config:
    """pyproject.tomlとglobal設定ファイルから設定を読み込む。

    `config_dir`配下の`pyproject.toml`の`[tool.pyfltr]`と、
    XDG準拠のglobal設定ファイル`~/.config/pyfltr/config.toml`の`[tool.pyfltr]`を
    1つの入力dictへマージしてから、preset反映・custom-commands登録・
    言語カテゴリゲート・通常設定適用の順で処理する。

    マージ仕様:
      - 通常キーはproject優先（後勝ち）
      - archive/cache系（`GLOBAL_PRIORITY_KEYS`）はglobal優先
    `pyproject.toml`不在でもglobal側のみ書かれていれば反映される
    （旧版にあった「pyproject.toml不在時の早期return」は撤廃済み）。

    Args:
        config_dir: project側の`pyproject.toml`を探すディレクトリ。
            未指定時はカレントディレクトリ。
        global_config_path: global設定ファイルのパス。未指定時は
            `default_global_config_path()`の結果を使用する。
    """
    config = create_default_config()
    base = config_dir or pathlib.Path.cwd()

    # global側の読み込み（不在時は空dict）
    if global_config_path is None:
        global_config_path = default_global_config_path()
    global_data = _read_global_config(global_config_path)

    # project側の読み込み（不在時は空dict）。
    # 旧実装にあった「pyproject.toml不在時の早期return」は撤廃済み。
    # globalだけが書かれている場合でも反映を成立させるため、
    # 不在時は空dictとして処理を継続する。
    pyproject_path = (base / "pyproject.toml").absolute()
    project_data: dict[str, typing.Any] = {}
    if pyproject_path.exists():
        text = pyproject_path.read_text(encoding="utf-8")
        try:
            pyproject_doc = tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as e:
            raise ValueError(f"pyproject.tomlのTOMLが不正です: {pyproject_path}: {e}") from e
        raw = pyproject_doc.get("tool", {})
        raw = raw.get("pyfltr", {}) if isinstance(raw, dict) else {}
        project_data = _unwrap_tomlkit(raw) if isinstance(raw, dict) else {}

    # global / projectをマージ。各キーの由来も記録する。
    tool_pyfltr, key_sources = _merge_global_and_project(global_data, project_data)

    # archive/cache系がproject側に書かれていた場合の警告。
    # global側にも当該キーがある場合のみ警告対象（global側に無ければproject値が
    # そのまま採用されるので警告不要）。
    priority_keys_overridden_by_global = sorted(
        key
        for key in GLOBAL_PRIORITY_KEYS
        if "project" in key_sources.get(key, set()) and "global" in key_sources.get(key, set())
    )
    if priority_keys_overridden_by_global:
        keys_str = ", ".join(priority_keys_overridden_by_global)
        pyfltr.warnings_.emit_warning(
            source="config",
            message=(f"archive/cache系のキーはglobal設定が優先されるため、project側の値は無視されます: {keys_str}"),
        )

    _apply_preset(config, tool_pyfltr)
    _register_custom_commands(config, tool_pyfltr)
    _apply_language_gate(config, tool_pyfltr)
    _normalize_config_values(config, tool_pyfltr, key_sources)
    _validate_config(config)
    _recompute_fast_aliases(config)
    _warn_config_files(config, base)

    return config


def _apply_preset(config: Config, tool_pyfltr: dict[str, typing.Any]) -> None:
    """presetキーを読み取り、対応するプリセット設定をconfigに反映する。"""
    preset = str(tool_pyfltr.get("preset", ""))
    if preset == "":
        return
    if preset in _PRESETS:
        config.values.update(_PRESETS[preset])
    elif preset in _REMOVED_PRESETS:
        raise ValueError(_REMOVED_PRESETS[preset])
    else:
        raise ValueError(f"preset の設定値が正しくありません。{preset=}")


def _register_custom_commands(config: Config, tool_pyfltr: dict[str, typing.Any]) -> None:
    """custom-commandsエントリを読み取り、各カスタムコマンドをconfigに登録する。"""
    custom_commands = tool_pyfltr.get("custom-commands", {})
    if not isinstance(custom_commands, dict):
        raise ValueError("custom-commandsはテーブルで指定してください")
    for name, definition in custom_commands.items():
        name = name.replace("_", "-")
        _register_custom_command(config, name, definition)


def _apply_language_gate(config: Config, tool_pyfltr: dict[str, typing.Any]) -> None:
    """言語カテゴリgateを適用する（preset < 言語カテゴリgate < 個別設定）。

    v3.0.0でpython / javascript / rust / dotnetを同じ枠組みのカテゴリキーに統一した。
    presetは各時点の推奨構成として全言語のツールを横断的にTrueにするが、カテゴリ
    キーがFalse（既定）のときはpreset由来のTrueをFalseへ上書きして実行を抑止する。
    後続の個別設定ループで`{command} = true` / `{command} = false`による上書きが可能
    （個別指定はgateを越えて最優先）。
    """
    user_keys = set(tool_pyfltr.keys())
    for category_key, commands in LANGUAGE_CATEGORIES:
        if bool(tool_pyfltr.get(category_key, False)):
            continue  # gate 開放: preset 由来の True をそのまま通す
        for cmd in commands:
            if cmd in user_keys:
                continue  # 個別設定による明示指定を保持 (True/False 双方)
            config.values[cmd] = False


def _normalize_config_values(
    config: Config,
    tool_pyfltr: dict[str, typing.Any],
    key_sources: dict[str, set[str]],
) -> None:
    """プリセット・言語カテゴリ以外の設定を適用し、targets/extend-targetsを反映する。

    プリセットと重複するキーは上書きされる。
    global由来のみで未知のキーは警告して無視する（前方互換性確保）。
    """
    skip_keys = ("custom-commands", *(key for key, _ in LANGUAGE_CATEGORIES))
    targets_overrides: dict[str, str | list[str]] = {}
    extend_targets_map: dict[str, str | list[str]] = {}
    global_only_unknown_keys: list[str] = []

    for key, value in tool_pyfltr.items():
        if key in skip_keys:
            continue  # 別途処理済み
        # v3.0.0で削除されたツール名に紐づく設定キーを検出したら移行案内を表示する。
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
            # 未知キーの由来別分岐（前方互換性確保）。
            # global由来のみのキーは新版pyfltrで追加された設定の可能性があるため、
            # 旧版でも停止せずに警告して無視する。projectは当該プロジェクトに紐づく
            # バージョンが既知のため従来通り厳格バリデーションを維持する。
            sources = key_sources.get(key, set())
            if sources == {"global"}:
                global_only_unknown_keys.append(key)
                continue
            raise ValueError(f"設定キーが不正です: {key}")
        if not isinstance(value, type(config.values[key])):  # 簡易チェック
            raise ValueError(f"設定値が不正です: {key}={type(value)}, expected {type(config.values[key])}")
        config.values[key] = value

    if global_only_unknown_keys:
        keys_str = ", ".join(sorted(global_only_unknown_keys))
        pyfltr.warnings_.emit_warning(
            source="config",
            message=f"global設定の未知キーを無視しました: {keys_str}",
        )

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


def _validate_config(config: Config) -> None:
    """Runner / severity / hintsのバリデーションを行う。"""
    # グローバルrunner設定（python-runner / js-runner / bin-runner）の値バリデーション。
    # カテゴリごとに許容値が異なるため、共通dispatcher構造で1箇所に集約する。
    _global_runner_specs: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("python-runner", PYTHON_RUNNERS),
        ("js-runner", JS_RUNNERS),
        ("bin-runner", BIN_RUNNERS),
    )
    for runner_key, allowed in _global_runner_specs:
        runner_value = config.values[runner_key]
        if runner_value not in allowed:
            raise ValueError(f"{runner_key}の設定値が正しくありません。{runner_value=} (許容値: {', '.join(allowed)})")

    # per-tool {command}-runnerの値バリデーション。
    # 対称12値のいずれかを許容する。カテゴリ横断の組み合わせ（例: Python系ツールに`pnpm`を指定）は
    # 拒否しない方針（実装簡潔さを優先し、無意味な組み合わせは実行時の解決ロジックがエラー終了する）。
    _global_runner_keys = frozenset(key for key, _ in _global_runner_specs)
    for key, value in config.values.items():
        if not key.endswith("-runner") or key in _global_runner_keys:
            continue
        if value not in COMMAND_RUNNERS:
            raise ValueError(f"{key}の設定値が正しくありません。{value=!r} (許容値: {', '.join(COMMAND_RUNNERS)})")

    # per-tool {command}-severityの値バリデーション。
    # ビルトイン分は既定値 "error" が登録済みでも、利用者が pyproject.toml で
    # 別値を書いた場合は本ループで検出する。カスタムコマンド側は
    # `_register_custom_command` で登録時に検証済みのため、ここでは値のみ確認する。
    for key, value in config.values.items():
        if not key.endswith("-severity"):
            continue
        if value not in SEVERITY_VALUES:
            raise ValueError(f"{key}の設定値が正しくありません。{value=!r} (許容値: {', '.join(SEVERITY_VALUES)})")

    # per-tool {command}-hintsの要素型バリデーション。
    # 上位の汎用バリデーション（list型一致）はパスするが、要素がstrでなければ
    # JSONL出力時に文字列前提のレコード組み立てが失敗するため、ここで明示的に
    # 文字列リストであることを確認する。
    for key, value in config.values.items():
        if not key.endswith("-hints"):
            continue
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key}は文字列のリストで指定してください: {value!r}")


def _recompute_fast_aliases(config: Config) -> None:
    """per-command fastフラグからfastエイリアスを再計算する。"""
    config.values["aliases"]["fast"] = _build_fast_alias(config)


def _warn_config_files(config: Config, base: pathlib.Path) -> None:
    """有効化されているコマンドで`CommandInfo.config_files`を満たさないものを警告する。"""
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

    # severity（省略時は "error"）。許容値以外はValueError。
    raw_severity: typing.Any = definition.get("severity", "error")
    if raw_severity not in SEVERITY_VALUES:
        raise ValueError(
            f"カスタムコマンド {name} のseverityは {SEVERITY_VALUES} のいずれかで指定してください: {raw_severity!r}"
        )
    severity: str = str(raw_severity)

    # hints（省略時は空リスト。要素はstr）。
    raw_hints: typing.Any = definition.get("hints", [])
    if not isinstance(raw_hints, list) or not all(isinstance(item, str) for item in raw_hints):
        raise ValueError(f"カスタムコマンド {name} のhintsは文字列のリストで指定してください")
    hints: list[str] = [str(item) for item in raw_hints]

    # values辞書にデフォルト設定を追加
    config.values[name] = True
    config.values[f"{name}-path"] = path
    config.values[f"{name}-args"] = args
    config.values[f"{name}-fast"] = fast
    config.values[f"{name}-pass-filenames"] = pass_filenames
    config.values[f"{name}-severity"] = severity
    config.values[f"{name}-hints"] = hints
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
    """fixステージで実行すべきコマンドに限定する。

    `pyfltr run` / `pyfltr fast`のfixステージはlinterのautofix機能
    （`{command}-fix-args`）を前段で呼び出すための段で、formatterは対象外。
    formatter本体は通常ステージで常に書き込みモードで動くため、fixステージで
    重複して実行する必要はない。

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


def read_config_values(path: pathlib.Path) -> dict[str, typing.Any]:
    """pyproject.tomlまたはglobal config.tomlから`[tool.pyfltr]`配下を返す。

    ファイル不在時は空辞書を返す。TOML構文エラー時は`ValueError`で停止する。
    `pyfltr config get` / `pyfltr config list`の読み取り経路で使用する。
    """
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        data = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as e:
        raise ValueError(f"設定ファイルのTOMLが不正です: {path}: {e}") from e
    raw = data.get("tool", {})
    raw = raw.get("pyfltr", {}) if isinstance(raw, dict) else {}
    return _unwrap_tomlkit(raw) if isinstance(raw, dict) else {}


def set_config_value(
    path: pathlib.Path,
    key: str,
    value: typing.Any,
    *,
    create_if_missing: bool = False,
) -> None:
    """設定ファイルの`[tool.pyfltr]`配下を更新する。

    既存ファイルはtomlkit経由で読み書きするためコメント・セクション順は保持される。
    `create_if_missing=True`なら、ファイル不在時にディレクトリ含めて新規作成する。
    `create_if_missing=False`でファイル不在なら`FileNotFoundError`を送出する。
    """
    if path.exists():
        text = path.read_text(encoding="utf-8")
        try:
            doc = tomlkit.parse(text)
        except tomlkit.exceptions.TOMLKitError as e:
            raise ValueError(f"設定ファイルのTOMLが不正です: {path}: {e}") from e
    else:
        if not create_if_missing:
            raise FileNotFoundError(f"設定ファイルが存在しません: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = tomlkit.document()

    tool_table = doc.get("tool")
    if tool_table is None:
        tool_table = tomlkit.table()
        doc["tool"] = tool_table
    pyfltr_table = tool_table.get("pyfltr")
    if pyfltr_table is None:
        pyfltr_table = tomlkit.table()
        tool_table["pyfltr"] = pyfltr_table

    pyfltr_table[key] = value
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def delete_config_value(path: pathlib.Path, key: str) -> bool:
    """設定ファイルから`[tool.pyfltr]`配下のキーを削除する。

    存在したかをboolで返す。セクションが空になっても削除しない
    （手書きコメントを保持するため）。
    ファイル不在時は`False`を返す。
    """
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    try:
        doc = tomlkit.parse(text)
    except tomlkit.exceptions.TOMLKitError as e:
        raise ValueError(f"設定ファイルのTOMLが不正です: {path}: {e}") from e
    tool_table = doc.get("tool")
    if tool_table is None:
        return False
    pyfltr_table = tool_table.get("pyfltr")
    if pyfltr_table is None:
        return False
    if key not in pyfltr_table:
        return False
    del pyfltr_table[key]
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return True


def parse_config_value(key: str, raw: str) -> typing.Any:
    """文字列値を`DEFAULT_CONFIG[key]`の型に変換する。

    bool / int / str / `list[str]`のみ対応。dict系（`aliases`等）は非対応で
    `ValueError`を送出する（CLI経由でdict編集はサポートしない方針）。

    - bool: `true`/`false`/`1`/`0`を受理（大文字小文字は無視）
    - int: `int(raw)`、失敗で`ValueError`
    - str: そのまま
    - list[str]: カンマ区切りでsplit、要素のtrimは行わない
      （`*-args`系で空白を含むケースに対応するため）
    """
    if key not in DEFAULT_CONFIG:
        raise ValueError(f"設定キーが不正です: {key}")
    default = DEFAULT_CONFIG[key]
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        raise ValueError(f"{key}にはtrue/false/1/0のいずれかを指定してください: {raw!r}")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError as e:
            raise ValueError(f"{key}には整数を指定してください: {raw!r}") from e
    if isinstance(default, str):
        return raw
    if isinstance(default, list):
        return raw.split(",") if raw else []
    if isinstance(default, dict):
        raise ValueError(f"{key}は辞書型のためCLIから直接設定できません。pyproject.tomlで編集してください")
    raise ValueError(f"{key}の値型はCLI経由では設定できません: {type(default).__name__}")
