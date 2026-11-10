"""document_lintersのツール登録。"""

import sys

from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_types import BinToolSpec, CommandInfo, FunctionRef, StructuredOutputSpec

COMMANDS: dict[str, CommandInfo] = {
    "markdownlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=0.9,
        per_file_cost=0.035,
        # 外部パス（起点cwd配下にない絶対パス）を渡すとmarkdownlint-cli2側で
        # 対象探索エラーが未整形のまま出力されるため除外＋警告経路を適用する。
        allows_external_paths=False,
        # `--config`が受け付ける候補（markdownlint-cli2公式README参照）。
        # `config_files`は登録しない（登録すると`warn_config_files`が
        # 設定不在時に警告を発行してしまい、外部パスのみ検査するケースで
        # 「プロジェクト側に設定不要」の運用と矛盾するため）。
        config_arg_template=["--config", "{path}"],
        config_inject_candidates=[
            ".markdownlint-cli2.jsonc",
            ".markdownlint-cli2.yaml",
            ".markdownlint-cli2.cjs",
            ".markdownlint-cli2.mjs",
            ".markdownlint.jsonc",
            ".markdownlint.json",
            ".markdownlint.yaml",
            ".markdownlint.yml",
            ".markdownlint.cjs",
            ".markdownlint.mjs",
        ],
        js_bin="markdownlint-cli2",
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<col>\d+))?\s+(?:\w+\s+)?(?P<rule>MD\d+)(?P<message>\S*\s+.+)",
        rule_url_builder=FunctionRef[[str, str | None], str | None]("pyfltr.rule_urls", "build_markdownlint_url"),
        defaults={
            "markdownlint": False,
            # ユーザーが明示的にpathを設定した場合はその値をそのまま使い、args先頭に自動prefixを追加しない。
            "markdownlint-path": "",
            "markdownlint-args": [],
            "markdownlint-runner": "js-runner",
            "markdownlint-fast": True,
            # fixステージ（pyfltr run / fastの自動修正段）で通常argsの後に追加する引数。
            # markdownlint-cli2は--fixでファイルをin-place修正する。
            "markdownlint-fix-args": ["--fix"],
        },
    ),
    "textlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=2.3,
        per_file_cost=0.4,
        # 外部パス（起点cwd配下にない絶対パス）を渡すとtextlint側で`SearchFilesNoTargetFileError`
        # の未整形スタックトレースが出力されるため除外＋警告経路を適用する。
        allows_external_paths=False,
        # textlintは対象ファイル単独で完結する解析を行い、設定ファイルもCLIから起動した場合は
        # CWD直下でのみ解決される（公式ドキュメントのconfiguring / ignore章に準拠）。
        # 以下はtextlintが自動で読み込む設定ファイルとignoreファイルの完全列挙。
        config_files=[
            ".textlintrc",
            ".textlintrc.json",
            ".textlintrc.yml",
            ".textlintrc.yaml",
            ".textlintrc.js",
            ".textlintrc.cjs",
            "package.json",
            ".textlintignore",
        ],
        # `--config`が受け付ける候補（textlint CLI仕様）。
        # `.textlintignore`・`package.json`は`--config`では渡せないため除外する。
        config_arg_template=["--config", "{path}"],
        config_inject_candidates=[
            ".textlintrc",
            ".textlintrc.json",
            ".textlintrc.yml",
            ".textlintrc.yaml",
            ".textlintrc.js",
            ".textlintrc.cjs",
        ],
        cacheable=True,
        js_bin="textlint",
        pnpx_package="textlint@<15.5.3 || >15.5.3",
        diagnostic_pattern=r"^(?P<file>.+?):\s*line\s+(?P<line>\d+),\s*col\s+(?P<col>\d+),\s*\w+\s*-\s*(?P<message>.+)",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.textlint", "parse_textlint_json"),
        structured_output=(
            "textlint-json",
            StructuredOutputSpec(
                inject=["--format", "json"],
                conflicts=["--format"],
                lint_only=True,
            ),
        ),
        rule_hints={
            # max-comma 4.0.0は文内の位置を段落内の位置として報告する。
            # 修正版の公開と、既定presetで修正版が解決され位置が正しくなる実観測の両方を確認したら撤去する。
            "ja-technical-writing/max-comma": (
                "max-comma 4.0.0 reports sentence-relative offsets as paragraph-relative positions."
                " From the second sentence, the location may point earlier, even to a preceding line."
                " Find the sentence exceeding the comma limit in the same paragraph."
            ),
            "ja-technical-writing/sentence-length": (
                "textlint counts up to the period (。) as one sentence; bullet-line splits still count as one."
                " Split with periods to shorten."
            ),
            "ja-technical-writing/max-ten": (
                "Too many commas (、) in one sentence; split into multiple sentences or revise conjunctions and dependencies."
            ),
            "ja-technical-writing/max-kanji-continuous-len": (
                "Long kanji run detected; insert hiragana, particles, or commas (、) to break it up."
            ),
            "ja-technical-writing/no-unmatched-pair": (
                "Bracket pair is unmatched. Check for typos or missing pairs, and ensure full-width bracket pairs"
                " do not span across line breaks (the rule treats line breaks as separators)."
            ),
        },
        defaults={
            "textlint-json": True,
            "textlint": False,
            "textlint-path": "",
            "textlint-runner": "js-runner",
            # lint / fix共通で常に付与される引数。lint専用オプション（--formatなど）はここではなく
            # textlint-lint-argsに書くこと。fix時は@textlint/fixer-formatterが使用されるが
            # compactフォーマッタが存在しないため、--format compactを共通argsに含めるとfixが失敗する。
            "textlint-args": [],
            # 非fixモード（およびfixモードの後段lintチェック）でのみ付与する引数。
            # 既定はcompactフォーマッタ指定。ただし既定で有効なtextlint-jsonが--format jsonを
            # 注入して既存の--format指定を除去するため、既定構成ではこの値は実効しない。
            "textlint-lint-args": ["--format", "compact"],
            # textlint向けルール / プリセットパッケージの列挙。pnpx / npxモードでは
            # --package / -p展開される。pnpm / npm / yarn / directモードでは
            # package.json側で管理する前提のため無視される。
            "textlint-packages": [
                "textlint-rule-preset-ja-technical-writing",
                "textlint-rule-preset-jtf-style",
                "textlint-rule-ja-no-abusage",
                "textlint-rule-preset-ai-words-ja",
            ],
            "textlint-fast": True,
            # fixモード時に通常argsの後に追加する引数。
            # textlintは--fixでautofix可能なルールをin-place修正する。
            "textlint-fix-args": ["--fix"],
            # fixモード実行で「破損させてはならない識別子」を列挙する。
            # textlint --fixの自動修正がコードブロック外の`.NET` / `Node.js`等の識別子まで
            # 変換してしまうことがあるため、fix前後で識別子が失われたケースを検知して警告を発行する。
            # 空リスト（`[]`）を指定すると検知を無効化できる。
            "textlint-protected-identifiers": [".NET", "Node.js", "Vue.js", "Next.js", "Nuxt.js"],
        },
        fix_execution_kind="textlint-fix",
    ),
    # designmd: @google/design.md による DESIGN.md 形式仕様チェック。
    # 対象ファイル名はDESIGN.md固定（公式仕様）。js-runner経由でnpmパッケージから起動する。
    "designmd": CommandInfo(
        type="linter",
        targets="DESIGN.md",
        fixed_cost=1.5,
        js_bin="design.md",
        pnpx_package="@google/design.md",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_designmd_json"),
        defaults={
            # designmd: @google/design.md による DESIGN.md 形式仕様チェック。js-runner経由。
            # 対象ファイルがあれば自動的に有効化される設計のため既定True。
            "designmd": True,
            "designmd-path": "",
            "designmd-runner": "js-runner",
            # `@google/design.md` の起動形式は `design.md lint <files>`。サブコマンドは共通argsに含める。
            "designmd-args": ["lint"],
            "designmd-fast": False,
        },
    ),
    # lychee: Rust製リンク切れチェッカー。bin-runner経由（mise）で起動する。
    # 外部URLへの到達性検証を主用途とするため、ネットワーク到達性に依存する。
    "lychee": CommandInfo(
        type="linter",
        targets=["*.md", "*.html"],
        fixed_cost=1.0,
        per_file_cost=0.05,
        auto_value_args=[("lychee-max-concurrency", "--max-concurrency={value}")],
        bin_spec=BinToolSpec(bin_name="lychee", mise_backend="github:lycheeverse/lychee"),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_lychee_json"),
        defaults={
            # lychee: Rust製リンク切れチェッカー。bin-runner経由（mise）。既定で有効。
            # 既定argsに`--offline`は加えない（外部URL検証が本来の用途のため）。
            # 5xx・応答タイムアウトだけの失敗は有限再試行後に警告化し、404等は失敗を維持する。
            "lychee": True,
            "lychee-path": "",
            "lychee-runner": "bin-runner",
            "lychee-args": ["--format", "json", "--no-progress"],
            "lychee-version": "latest",
            "lychee-fast": False,
            # lycheeの同時接続数（`--max-concurrency`）。0以下でlychee既定値（128）へ委ねる。
            # GitHub Actions上のlycheeがgithub.com宛リンクに対して
            # `Network error: HTTP/2 protocol error. Server may not support HTTP/2 properly`で
            # 失敗する事象を観測したため、既定値を4へ抑える。
            # 同時接続数が原因であるという一次資料上の裏付けは無いが、
            # 同時接続数を抑えた実行では対象の失敗が再現していないため緩和策として採用する。
            # 利用者側の`{command}-extend-args`での個別対処を不要にする目的でpyfltr既定に置く。
            "lychee-max-concurrency": 4,
        },
    ),
    # colloquial-check: 日本語文書の口語表現を検出する内蔵linter。
    # LLMが出力しがちな口語的な言い回しを`warning`として指摘する（既定で無効・opt-in）。
    # 起動経路は`python -m pyfltr.colloquial`。
    "colloquial-check": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=0.1,
        per_file_cost=0.005,
        subproject_aware=False,
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
        defaults={
            # colloquial-check: 日本語文書の口語表現を検出する内蔵linter。既定で無効（opt-in）。
            # 検出結果はseverity既定"warning"によりCI/pre-commitを失敗させない。
            # `{command}-path`は実行ファイルパス（文字列）を1件のみ許容するため、`python -m`起動は
            # `-path`にインタープリターを、`-args`にモジュール指定を分けて渡す。
            "colloquial-check": False,
            "colloquial-check-path": sys.executable,
            "colloquial-check-args": ["-m", "pyfltr.colloquial"],
            "colloquial-check-runner": "direct",
            "colloquial-check-fast": True,
            "colloquial-check-severity": "warning",
        },
    ),
}
