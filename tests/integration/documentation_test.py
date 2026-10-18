"""利用ガイドに写した一覧と公開定義の整合。"""

import argparse
import ast
import dataclasses
import inspect
import pathlib
import re
import textwrap
import typing

import pytest
import tomlkit

import pyfltr.cli.command_info
import pyfltr.cli.mcp_server
import pyfltr.cli.output_format
import pyfltr.cli.parser
import pyfltr.cli.runs
import pyfltr.command.completion
import pyfltr.command.core_
import pyfltr.command.subprojects
import pyfltr.config.model
import pyfltr.config.presets
import pyfltr.config.validation
import pyfltr.grep_.adaptive
import pyfltr.grep_.preview
import pyfltr.grep_.scanner
import pyfltr.grep_.types
import pyfltr.output.jsonl
import pyfltr.parsing.common
import pyfltr.tools

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _guide(name: str) -> str:
    return (ROOT / "docs/guide" / name).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    return source.split(start, 1)[1].split(end, 1)[0]


def _tool_names(source: str) -> set[str]:
    for name, info in pyfltr.tools.BUILTIN_COMMANDS.items():
        source = source.replace(name.replace("-", " "), name)
        if info.js_bin is not None:
            source = source.replace(info.js_bin, name)
    # 文書側の未知名も残し、廃止されたツールが一覧へ残る場合を検出する。
    return set(re.findall(r"(?<![A-Za-z0-9_-])[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?![A-Za-z0-9_-])", source))


def _subparsers() -> dict[str, argparse.ArgumentParser]:
    parser = pyfltr.cli.parser.build_parser()
    return _nested_parsers(parser)


def _arguments(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    # argparseの登録済み引数一覧は内部属性から取得する。
    return parser._actions  # pylint: disable=protected-access


def _nested_parsers(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in _arguments(parser):
        # サブコマンド登録の公開APIに対応するアクション型を判別する。
        if isinstance(action, argparse._SubParsersAction):  # pylint: disable=protected-access
            return action.choices
    raise AssertionError("サブコマンドの登録が見つからない")


def _choices(command: str, option: str) -> set[str]:
    action = next(action for action in _arguments(_subparsers()[command]) if option in action.option_strings)
    assert action.choices is not None
    return set(action.choices)


def _codes(source: str) -> set[str]:
    return set(re.findall(r"(?<!`)`([^`\n]+)`(?!`)", source))


def test_documented_error_pattern_groups_match_definition() -> None:
    source = _guide("custom-commands.md")
    required = _between(source, "`error-pattern`は名前付きグループ", "が必須。")
    optional = _between(source, "任意のグループは", "である。")
    table = _between(source, "| グループ |", "ビルトインコマンド（")
    assert _codes(required) == set(pyfltr.parsing.common.REQUIRED_ERROR_PATTERN_GROUPS)
    assert _codes(optional) == set(pyfltr.parsing.common.OPTIONAL_ERROR_PATTERN_GROUPS)
    assert set(re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE)) == set(pyfltr.parsing.common.OPTIONAL_ERROR_PATTERN_GROUPS)


def test_documented_custom_command_keys_match_validation() -> None:
    section = _between(_guide("custom-commands.md"), "設定項目。", "`error-pattern`の任意グループは")
    documented = set(re.findall(r"^- `([^`]+)`", section, re.MULTILINE))
    # 受理キーはdefinition.getへ分散しているため、実装のASTから取得する。
    tree = ast.parse(inspect.getsource(pyfltr.config.validation.register_custom_command))
    expected = {
        node.args[0].value.replace("_", "-")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "definition"
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert expected, "受理キーの取得方式を実装の定義に合わせて更新する"
    assert documented == expected
    type_row = section.split("\n", 2)[2].split("\n", 1)[0]
    assert _codes(type_row) - {"type"} == {f'"{value}"' for value in typing.get_args(pyfltr.tools.CommandType)}


def test_documented_subcommands_and_config_actions_match_parser() -> None:
    source = _guide("usage.md")
    documented = set(re.findall(r"^### サブコマンド: ([a-z-]+)", source, re.MULTILINE))
    parsers = _subparsers()
    assert documented == set(parsers)
    actions = _nested_parsers(parsers["config"])
    assert set(re.findall(r"^#### config ([a-z]+)", source, re.MULTILINE)) == set(actions)
    types = _between(source, "値の文字列はキーのデフォルト値の型", "```shell")
    assert {value.split("[", 1)[0] for value in re.findall(r"^- `([^`]+)`:", types, re.MULTILINE)} == {
        type(value).__name__ for value in pyfltr.config.model.DEFAULT_CONFIG.values()
    }


@pytest.mark.parametrize("command", ["list-runs", "show-run", "command-info", "grep", "replace"])
def test_documented_auxiliary_formats_match_parser(command: str) -> None:
    source = _guide("usage.md")
    section = _between(source, f"### サブコマンド: {command}", "### ")
    if command in {"grep", "replace"}:
        section = _between(_guide("grep-replace.md"), f"### {command}オプション", "### ")
    match = re.search(r"--output-format[ =]([a-z|]+)", section)
    assert match is not None
    assert set(match.group(1).split("|")) == _choices(command, "--output-format")


def test_documented_completion_values_match_type() -> None:
    section = _between(_guide("usage.md"), "`run`の応答は次の順に読む。", "`exit_code`は")
    expected = set(typing.get_args(pyfltr.command.completion.CompletionStatus))
    assert _codes(section) & expected == expected


def test_documented_external_path_exclusions_match_definitions() -> None:
    section = _between(_guide("usage.md"), "- 外部パス除外＋警告:", "- 既定（素通し）")
    assert _codes(section) == {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if not info.allows_external_paths}


def test_documented_output_formats_and_agent_envs_match_definitions() -> None:
    source = _guide("usage.md")
    table = _between(source, "| 値 | 用途 | 動作 |", "`jsonl`はツール完了順")
    assert set(re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE)) == _choices("run", "--output-format")
    envs = _between(source, "環境変数`AI_AGENT`", "のいずれかが設定されていれば")
    assert _codes("`AI_AGENT`" + envs) == set(pyfltr.cli.output_format.AGENT_INDICATOR_ENVS)
    section = _between(source, "引数にシェル種別（", "）を指定する")
    shells = next(action for action in _arguments(_subparsers()["generate-shell-completion"]) if action.dest == "shell")
    assert shells.choices is not None
    assert _codes(section) == set(shells.choices)


def test_documented_monorepo_markers_and_exclusions_match_definitions() -> None:
    source = _guide("usage.md")
    markers = _between(source, "マーカーは固定ファイル名", "とする。")
    # 検出に用いる共通定義を参照し、一覧に新しい検出対象が欠けることを検出する。
    names = pyfltr.command.subprojects._SUBPROJECT_MARKER_NAMES  # pylint: disable=protected-access
    globs = pyfltr.command.subprojects._SUBPROJECT_MARKER_GLOBS  # pylint: disable=protected-access
    expected = set(names + globs)
    assert _codes(markers) == expected
    excluded = _between(source, "`.venv`・`node_modules`・`target`・`build`・`dist`・`.git`配下", "走査しない")
    # 走査から除外される名前の定義との一致を検査する。
    excluded_names = pyfltr.command.subprojects._DEFAULT_EXCLUDE_NAMES  # pylint: disable=protected-access
    assert _codes("`.venv`・`node_modules`・`target`・`build`・`dist`・`.git`配下" + excluded) == set(excluded_names)


def test_documented_cache_tools_match_definitions() -> None:
    section = _between(_guide("usage.md"), "## ファイルhashキャッシュ", "## モノレポでの動作")
    description = _between(section, "現時点の対象は", "のみである。")
    assert _tool_names(description) == {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.cacheable}


def test_documented_preset_names_match_definitions() -> None:
    section = _between(_guide("configuration.md"), '`"latest"`または日付指定', "を指定する。")
    assert set(re.findall(r'`"([^"`]+)"`', '`"latest"`' + section)) == set(pyfltr.config.presets.preset_names())


@pytest.mark.parametrize(("language", "expected"), pyfltr.tools.LANGUAGE_CATEGORIES)
def test_documented_language_categories_match_definitions(language: str, expected: tuple[str, ...]) -> None:
    source = _guide("configuration.md")
    line = next(line for line in source.splitlines() if line.startswith(f"- `{language}`:"))
    names = re.sub(r"（[^）]*）", "", line.split(":", 1)[1])
    assert _tool_names(names) == set(expected)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("python-runner", pyfltr.tools.PYTHON_RUNNERS),
        ("js-runner", pyfltr.tools.JS_RUNNERS),
        ("bin-runner", pyfltr.tools.BIN_RUNNERS),
    ],
)
def test_documented_runner_values_match_definitions(key: str, expected: tuple[str, ...]) -> None:
    line = next(line for line in _guide("configuration.md").splitlines() if line.startswith(f"- {key} :"))
    assert set(re.findall(r'`"([^"`]+)"`', line)) == set(expected)


def test_documented_global_priority_severity_and_expanduser_match_definitions() -> None:
    source = _guide("configuration.md")
    section = _between(source, "- archive系（", "これらをproject側")
    assert _codes(section) == set(pyfltr.config.model.GLOBAL_PRIORITY_KEYS)
    section = _between(source, "許容値は", "の2値。")
    assert set(re.findall(r'`"([^"`]+)"`', section)) == set(pyfltr.config.model.SEVERITY_VALUES)
    section = _between(source, "対応キーは以下。", "```toml")
    suffixes = {value.removeprefix("{command}") for value in _codes(section) if value.startswith("{command}")}
    assert suffixes == set(pyfltr.config.model.EXPAND_USER_KEY_SUFFIXES)


def test_documented_auto_options_match_definitions() -> None:
    section = _between(_guide("configuration.md"), "| 設定 | 既定 | 自動追加される引数 |", "自動引数は")
    expected = {key for args in pyfltr.tools.AUTO_ARGS.values() for key, _ in args} | {
        key for args in pyfltr.tools.AUTO_VALUE_ARGS.values() for key, _ in args
    }
    assert set(re.findall(r"^\| `([^`]+)` \|", section, re.MULTILINE)) == expected


def test_documented_grep_types_and_output_modes_match_definitions() -> None:
    source = _guide("grep-replace.md")
    types = _between(source, "言語タイプフィルタ（", "）")
    # タイプ名の受理集合を所有する内部定義との一致を検査する。
    type_patterns = pyfltr.grep_.scanner._TYPE_PATTERNS  # pylint: disable=protected-access
    assert set(types.split("/")) == set(type_patterns)
    section = _between(source, "`output_mode`は選択された形式を示し、", "のいずれかとなる。")
    assert _codes(section) == set(typing.get_args(pyfltr.grep_.adaptive.OutputMode))
    section = _between(source, "summaryには", "が含まれる。")
    fields = {field.name for field in dataclasses.fields(pyfltr.grep_.adaptive.Selection)}
    assert _codes(section) == {name for name in fields if name.startswith(("returned_", "omitted_"))}
    section = _between(source, "- `truncated`:", "\n- `line_text_offset`")
    record = pyfltr.grep_.types.MatchRecord(pathlib.Path("example"), 1, 1, 4, "long", "long", ["long"], ["long"])
    assert _codes(section) == set(pyfltr.grep_.preview.build_match_preview(record, max_chars=1).truncated_fields)


@pytest.mark.parametrize("runner", ["python-runner", "uvx", "js-runner", "bin-runner"])
def test_documented_runner_tool_groups_match_defaults(runner: str) -> None:
    source = _guide("configuration-tools.md")
    marker = f"- {runner}経由" if runner != "uvx" else "- uvx既定"
    section = source.split(marker, 1)[1].split("\n- ", 1)[0].split("`{command}-runner`は", 1)[0]
    section = section.split("対象は", 1)[1]
    section = section.split("グローバル", 1)[0] if runner in {"python-runner", "bin-runner"} else section.split("。", 1)[0]
    section = re.sub(r"[a-z]+系", "", section)
    expected = {
        name for name in pyfltr.tools.BUILTIN_COMMANDS if pyfltr.config.model.DEFAULT_CONFIG[f"{name}-runner"] == runner
    }
    assert _tool_names(section) == expected


def test_documented_per_tool_runner_values_match_definitions() -> None:
    section = _between(_guide("configuration-tools.md"), "| 種別 | 値 | 解決方法 |", "個別ツール単位")
    documented = {
        value
        for line in section.splitlines()
        if line.startswith("| ")
        for value in re.findall(r'`"([^"`]+)"`', line.split("|", 3)[2])
    }
    assert documented == set(pyfltr.tools.COMMAND_RUNNERS)


def test_documented_bin_runner_enable_example_matches_definitions() -> None:
    section = _between(_guide("configuration-tools.md"), "### bin-runner対応ツールの設定", "既定の引数は以下")
    expected = {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.bin_spec is not None}
    documented = set(re.findall(r"^([a-z-]+) = true$", section, re.MULTILINE))
    assert documented == expected


def test_documented_tool_default_arguments_match_definitions() -> None:
    source = _guide("configuration-tools.md")
    # 既定引数として掲載した代入だけを拾い、上書き例を除く。
    snippets = []
    for line in source.splitlines():
        if line.startswith(("- ", "    - ", "既定の引数は", "pyfltr既定の", "既定の`")) and "上書き" not in line:
            snippets.extend(re.findall(r"`([a-z-]+(?:args|identifiers|packages) = \[[^`]+?\])`", line))
    assert snippets
    for snippet in snippets:
        for key, value in tomlkit.parse(snippet).items():
            assert value == pyfltr.config.model.DEFAULT_CONFIG[key], key
    for key in ("textlint-packages", "textlint-protected-identifiers"):
        section = source.split(f"{key} = [", 1)[1].split("]", 1)[0]
        assert tomlkit.parse(f"{key} = [{section}]")[key] == pyfltr.config.model.DEFAULT_CONFIG[key]


def test_documented_mise_backends_match_definitions() -> None:
    section = _between(_guide("configuration-tools.md"), "mise backendの既定値は、", "である。")
    documented = _codes(section)
    expected = {
        info.bin_spec.mise_backend
        for name, info in pyfltr.tools.BUILTIN_COMMANDS.items()
        if name.startswith(("cargo-", "dotnet-")) and info.bin_spec is not None
    }
    assert documented == expected


@pytest.mark.parametrize(
    ("heading", "language"), [("Python", "python"), ("JS/TS", "javascript"), ("Rust", "rust"), (".NET", "dotnet")]
)
def test_documented_guide_language_tools_match_definitions(heading: str, language: str) -> None:
    section = _guide("index.md").split(f"## {heading}系", 1)[1].split("\n## ", 1)[0]
    expected = {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.language == language}
    # 説明文の設定例を含めず、各分類の箇条書きを比べる。
    rows = "\n".join(line for line in section.splitlines() if line.startswith(("- Formatters:", "- Linters:", "- Testers:")))
    rows = re.sub(r"（[^）]*）", "", rows)
    assert _tool_names(rows) == expected
    for label, kind in (("Formatters", "formatter"), ("Linters", "linter"), ("Testers", "tester")):
        line = next(line for line in rows.splitlines() if line.startswith(f"- {label}:"))
        assert _tool_names(line) == {name for name in expected if pyfltr.tools.BUILTIN_COMMANDS[name].type == kind}


def test_documented_guide_tools_and_disabled_notes_match_definitions() -> None:
    source = _guide("index.md")
    documents = _between(source, "## ドキュメント系", "pyfltrの設定キー")
    others = _between(source, "## その他", "既定値が無効（opt-in）のツールは")
    others = others.split("- 統合:", 1)[0] + "- 統合:" + others.split("- 統合:", 1)[1].split("\n", 1)[0]
    rows = re.sub(r"（[^）]*）", "", documents + others)
    assert _tool_names(rows) == {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.language is None}
    # 全言語カテゴリは冒頭の無効注記、その他はツールごとの注記が対応する。
    for name in re.findall(r"([a-z]+(?:-[a-z]+)*)（(?:[^）]*、)?既定は無効）", source):
        assert not pyfltr.config.model.DEFAULT_CONFIG[name], name


def test_documented_removed_tools_match_definitions() -> None:
    section = _between(_guide("troubleshooting.md"), "v3.0.0で削除されたツール（", "）")
    assert set(section.split("・")) == set(pyfltr.tools.REMOVED_COMMANDS)


@pytest.mark.parametrize(
    ("heading", "runner"),
    [("Python系ツール", "python-runner"), ("uvx既定のPython製ツール", "uvx"), ("JS系ツール", "js-runner")],
)
def test_documented_troubleshooting_tool_categories_match_defaults(heading: str, runner: str) -> None:
    section = _guide("troubleshooting.md").split(f"### {heading}（", 1)[1].split("）", 1)[0]
    expected = {
        name for name in pyfltr.tools.BUILTIN_COMMANDS if pyfltr.config.model.DEFAULT_CONFIG[f"{name}-runner"] == runner
    }
    assert _tool_names(section) == expected


@pytest.mark.parametrize(
    ("page", "marker", "language"),
    [("recommended.md", "推奨ツール（", "python"), ("recommended-nonpython.md", "JS/TS系ツール一式（", "javascript")],
)
def test_documented_recommended_tools_match_presets(page: str, marker: str, language: str) -> None:
    section = _guide(page).split(marker, 1)[1].split("）", 1)[0]
    preset = pyfltr.config.presets.get_preset("latest")
    assert preset is not None
    expected = {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.language == language and preset.get(name)}
    assert _tool_names(section) == expected


def test_documented_run_history_columns_and_kinds_match_definitions() -> None:
    source = _guide("usage.md")
    # 列名はtext表示のheader定義、kindはJSONLの辞書定義から取得する。
    tree = ast.parse(inspect.getsource(pyfltr.cli.runs))
    headers = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "header" for target in node.targets)
    ]
    assert len(headers) == 1
    expected_header = set(ast.literal_eval(headers[0]))
    columns = _between(source, "固定幅テーブル。列は", "\n- `json`:")
    assert _codes(columns) == expected_header
    show_run_functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name != "execute_list_runs"]
    expected_kinds = {
        value.value
        for function in show_run_functions
        for node in ast.walk(function)
        if isinstance(node, ast.Dict)
        for key, value in zip(node.keys, node.values, strict=True)
        if isinstance(key, ast.Constant) and key.value == "kind" and isinstance(value, ast.Constant)
    }
    kinds = _between(source, "`kind`は`meta`", "いずれかとなる")
    assert _codes("`meta`" + kinds) == expected_kinds
    section = _between(source, "既定: `meta`（", "）と")
    # 既定metaの出力項目を組み立てる定義を参照する。
    summary_function = pyfltr.cli.runs._summary_to_dict  # pylint: disable=protected-access
    function_tree = ast.parse(inspect.getsource(summary_function))
    expected_meta = {
        key.value
        for node in ast.walk(function_tree)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant)
    }
    assert _codes(section) == expected_meta


def test_documented_quiet_header_matches_output() -> None:
    source = _guide("usage.md")
    section = _between(source, "- headerレコードを", "の3つのフィールドのみ")
    # quiet経路は環境のツール解決を起動せず、公開されたheader出力を検査できる。
    record = pyfltr.output.jsonl._build_header_record(  # pylint: disable=protected-access
        [], 0, run_id="example", quiet=True
    )
    assert _codes(section) == set(record) - {"kind"}


def test_documented_jsonl_status_values_match_definitions() -> None:
    source = _guide("usage.md")
    # statusの公開プロパティが返す値をASTから抽出する。
    prop = inspect.getattr_static(pyfltr.command.core_.CommandResult, "status")
    assert isinstance(prop, property)
    assert prop.fget is not None
    tree = ast.parse(textwrap.dedent(inspect.getsource(prop.fget)))
    expected = {
        node.value.value
        for node in ast.walk(tree)
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "status" for target in node.targets)
            or isinstance(node, ast.Return)
        )
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert expected
    section = _between(source, "- 成功時commandレコード", "- headerレコード")
    successful = _between(section, "`status`が", "のいずれか")
    assert _codes(successful) == expected - set(pyfltr.command.core_.RERUN_STATUSES)
    assert expected <= _codes(source)


def test_documented_fast_tools_match_defaults() -> None:
    section = _between(_guide("usage.md"), "内訳は以下。", "含まれるコマンドは")
    expected = {name for name in pyfltr.tools.BUILTIN_COMMANDS if pyfltr.config.model.DEFAULT_CONFIG[f"{name}-fast"]}
    documented = {value for value in _codes(section) if re.fullmatch(r"[a-z][a-z0-9-]*", value)}
    assert documented == expected


def test_documented_command_info_sections_match_output() -> None:
    section = _between(_guide("usage.md"), "出力はセクション見出し", "で関連項目をまとめる")
    # 見出しは出力処理の定義から取得し、文書側の列挙と独立に比較する。
    module = inspect.getsource(pyfltr.cli.command_info)
    expected = set(re.findall(r'"(## [^"\n]+)"', module))
    assert set(re.findall(r"`(## [^`]+)`", section)) == expected


@pytest.mark.parametrize("occurrence", [0, 1])
def test_documented_repository_tools_match_definitions(occurrence: int) -> None:
    section = _guide("usage.md").split("リポジトリ単位のツール（")[occurrence + 1].split("）", 1)[0]
    expected = {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if not info.subproject_aware}
    assert _codes(section) == expected


def test_documented_config_injection_tools_match_definitions() -> None:
    section = _between(_guide("usage.md"), "系の明示注入:", "\n- 外部パス除外")
    expected = {name for name, info in pyfltr.tools.BUILTIN_COMMANDS.items() if info.config_arg_template}
    assert _codes(section) == expected


def test_documented_preset_tools_match_definition() -> None:
    section = _between(_guide("configuration.md"), '### preset "20260413"', '### preset "20260411"')
    expected = pyfltr.config.presets.get_preset("20260413")
    assert expected is not None
    assert set(re.findall(r"^- `([a-z-]+) = true`", section, re.MULTILINE)) == {
        name for name, enabled in expected.items() if enabled
    }


@pytest.mark.parametrize("suffix", ["lint-args", "fix-args"])
def test_documented_mode_argument_tools_match_defaults(suffix: str) -> None:
    section = _guide("configuration.md").split(f"- {{command}}-{suffix} :", 1)[1].split("\n- ", 1)[0]
    section = re.split(r"既定(?:値)?は", section, maxsplit=1)[1].split("に定義", 1)[0]
    expected = {name for name in pyfltr.tools.BUILTIN_COMMANDS if f"{name}-{suffix}" in pyfltr.config.model.DEFAULT_CONFIG}
    assert _tool_names(section) == expected


def test_documented_global_config_keys_match_defaults() -> None:
    section = _between(_guide("configuration.md"), "- preset :", "`prettier-check-args`")
    documented = set(re.findall(r"^- ([a-z][a-z-]*) :", "- preset :" + section, re.MULTILINE))
    # ツール固有項目は同ページが参照するツール別設定の責務である。
    owners = tuple(f"{name}-" for name in pyfltr.tools.BUILTIN_COMMANDS)
    expected = {
        key
        for key in pyfltr.config.model.DEFAULT_CONFIG
        if key not in pyfltr.tools.BUILTIN_COMMANDS and not key.startswith(owners)
    }
    expected.discard("aliases")  # エイリアスはusage.mdの独立した一覧で検査する。
    assert documented <= pyfltr.config.model.DEFAULT_CONFIG.keys()
    assert {key for key in documented if not key.startswith(owners)} == expected


def test_documented_pass_filenames_tools_match_defaults() -> None:
    section = _between(_guide("configuration.md"), "以下のツールが`pass-filenames = false`", "pre-commit・prekの呼出し")
    expected = {
        name
        for name in pyfltr.tools.BUILTIN_COMMANDS
        if not pyfltr.config.model.DEFAULT_CONFIG.get(f"{name}-pass-filenames", True)
    }
    assert _codes(section) == expected


@pytest.mark.parametrize("occurrence", [0, 1])
def test_documented_direct_tools_match_defaults(occurrence: int) -> None:
    source = _guide("configuration-tools.md")
    section = source.split("対象はtypos", occurrence + 1)[-1].split("。", 1)[0]
    expected = {
        name for name in pyfltr.tools.BUILTIN_COMMANDS if pyfltr.config.model.DEFAULT_CONFIG[f"{name}-runner"] == "direct"
    }
    assert _tool_names("typos" + section) == expected


@pytest.mark.parametrize("command", ["grep", "replace"])
def test_documented_search_options_match_parser(command: str) -> None:
    source = _guide("grep-replace.md")
    section = _between(source, f"### {command}オプション", "### ")
    documented = set(re.findall(r"(?<![a-z-])--?[A-Za-z][A-Za-z-]*", section))
    parser = _subparsers()[command]
    expected = {option for action in _arguments(parser) for option in action.option_strings}
    expected.difference_update({"-h", "--help"})
    # 文書では前後文脈の3つの短縮形をまとめて掲載する。
    short_actions = {
        option
        for action in _arguments(parser)
        if any(len(option) == 2 for option in action.option_strings)
        for option in action.option_strings
    }
    assert documented <= expected
    assert expected - short_actions <= documented
    assert {option for option in expected if len(option) == 2} <= documented


@pytest.mark.asyncio
async def test_documented_replace_history_signature_matches_server() -> None:
    source = _guide("grep-replace.md")
    signature = re.search(r"`replace_history\(([^)]+)\)`", source)
    assert signature is not None
    documented = {part.strip().split("=", 1)[0] for part in signature.group(1).split(",")}
    tools = await pyfltr.cli.mcp_server.build_server().list_tools()
    tool = next(tool for tool in tools if tool.name == "replace_history")
    assert documented == set(tool.input_schema["properties"])


def documented_aliases(source: str) -> dict[str, list[str]]:
    """単独実行の節からエイリアス一覧を読み取る。"""
    section = source.split("## 特定のツールのみ実行", 1)[1].split("## UI", 1)[0]
    aliases: dict[str, list[str]] = {}
    for name in ("format", "lint", "test", "audit"):
        match = re.search(rf"^- `{name}`:(.*(?:\n    .*)*)", section, re.MULTILINE)
        assert match is not None, name
        aliases[name] = re.findall(r"`([^`]+)`", match.group(1))
    return aliases


def test_documented_aliases_match_defaults() -> None:
    source = (ROOT / "docs/guide/usage.md").read_text(encoding="utf-8")
    expected = pyfltr.config.model.DEFAULT_CONFIG["aliases"]
    assert documented_aliases(source) == {name: expected[name] for name in ("format", "lint", "test", "audit")}


@pytest.mark.asyncio
async def test_documented_mcp_tools_match_server() -> None:
    source = (ROOT / "docs/guide/usage.md").read_text(encoding="utf-8")
    table = source.split("提供するMCPツール", 1)[1].split("コーディングエージェント側への", 1)[0]
    documented = re.findall(r"^\| `([^`]+)` \|", table, re.MULTILINE)
    tools = await pyfltr.cli.mcp_server.build_server().list_tools()
    assert len(documented) == len(set(documented))
    assert set(documented) == {tool.name for tool in tools}
