import sys

from pyfltr.comment_check.check import SUFFIXES
from pyfltr.diagnostics import ErrorLocation
from pyfltr.tool_types import BinToolSpec, CommandInfo, FunctionRef, StructuredOutputSpec

COMMANDS: dict[str, CommandInfo] = {
    "markdownlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=0.9,
        per_file_cost=0.035,
        allows_external_paths=False,
        # `--config`が受け付ける候補（markdownlint-cli2公式README参照）。
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
            "markdownlint-path": "",
            "markdownlint-args": [],
            "markdownlint-runner": "js-runner",
            "markdownlint-fast": True,
            "markdownlint-fix-args": ["--fix"],
        },
    ),
    "textlint": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=2.3,
        per_file_cost=0.4,
        allows_external_paths=False,
        # CWD直下でのみ解決される（公式ドキュメントのconfiguring / ignore章に準拠）。
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
            "textlint-args": [],
            "textlint-lint-args": ["--format", "compact"],
            "textlint-packages": [
                "textlint-rule-preset-ja-technical-writing",
                "textlint-rule-preset-jtf-style",
                "textlint-rule-ja-no-abusage",
                "textlint-rule-preset-ai-words-ja",
            ],
            "textlint-fast": True,
            "textlint-fix-args": ["--fix"],
            "textlint-protected-identifiers": [".NET", "Node.js", "Vue.js", "Next.js", "Nuxt.js"],
        },
        fix_execution_kind="textlint-fix",
    ),
    # 対象ファイル名はDESIGN.md固定（公式仕様）。js-runner経由でnpmパッケージから起動する。
    "designmd": CommandInfo(
        type="linter",
        targets="DESIGN.md",
        fixed_cost=1.5,
        js_bin="design.md",
        pnpx_package="@google/design.md",
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_designmd_json"),
        defaults={
            "designmd": True,
            "designmd-path": "",
            "designmd-runner": "js-runner",
            "designmd-args": ["lint"],
            "designmd-fast": False,
        },
    ),
    "lychee": CommandInfo(
        type="linter",
        targets=["*.md", "*.html"],
        fixed_cost=1.0,
        per_file_cost=0.05,
        auto_value_args=[("lychee-max-concurrency", "--max-concurrency={value}")],
        bin_spec=BinToolSpec(bin_name="lychee", mise_backend="github:lycheeverse/lychee"),
        parser=FunctionRef[[str], list[ErrorLocation]]("pyfltr.parsing.tools", "parse_lychee_json"),
        defaults={
            "lychee": True,
            "lychee-path": "",
            "lychee-runner": "bin-runner",
            "lychee-args": ["--format", "json", "--no-progress"],
            "lychee-version": "latest",
            "lychee-fast": False,
            # GitHub Actions上のlycheeがgithub.com宛リンクに対して
            "lychee-max-concurrency": 4,
        },
    ),
    "colloquial-check": CommandInfo(
        type="linter",
        targets="*",
        fixed_cost=0.1,
        per_file_cost=0.005,
        subproject_aware=False,
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+):\s*(?P<message>.+)",
        defaults={
            "colloquial-check": False,
            "colloquial-check-path": sys.executable,
            "colloquial-check-args": ["-m", "pyfltr.colloquial"],
            "colloquial-check-runner": "direct",
            "colloquial-check-fast": True,
            "colloquial-check-severity": "warning",
        },
    ),
    "markdown-structure": CommandInfo(
        type="linter",
        targets="*.md",
        fixed_cost=0.2,
        per_file_cost=0.01,
        subproject_aware=False,
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<severity>error|warning): (?P<message>.+)$",
        defaults={
            "markdown-structure": False,
            "markdown-structure-path": sys.executable,
            "markdown-structure-args": ["-m", "pyfltr.markdown_structure"],
            "markdown-structure-runner": "direct",
            "markdown-structure-fast": True,
            "markdown-structure-fix-args": ["--fix"],
        },
    ),
    "comment-check": CommandInfo(
        type="linter",
        targets=[f"*{suffix}" for suffix in SUFFIXES],
        fixed_cost=0.1,
        per_file_cost=0.005,
        subproject_aware=False,
        diagnostic_pattern=r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): (?P<severity>error): (?P<message>.+)$",
        defaults={
            "comment-check": False,
            "comment-check-path": sys.executable,
            "comment-check-args": ["-m", "pyfltr.comment_check"],
            "comment-check-runner": "direct",
            "comment-check-fast": False,
        },
    ),
}
