import argparse
import ast
import functools
import io
import pathlib
import re
import tokenize
import warnings

import pyfltr.cli.parser

_REPO_ROOT = pathlib.Path(__file__).parents[2]


_PYTHON_ZONES = ("cli", "state", "grep_", "output", "colloquial")


_DOCUMENT_GLOBS = ("docs/**/*.md", "README.md")


_EXTERNAL_OPTIONS_BY_PATH: dict[str, frozenset[str]] = {
    "docs/guide/recommended-nonpython.md": frozenset({"--passWithNoTests"}),
    "docs/guide/recommended.md": frozenset({"--frozen"}),
    "docs/guide/usage.md": frozenset({"--config", "--frozen"}),
    "pyfltr/cli/command_info.py": frozenset({"--format", "--current", "--json"}),
    "pyfltr/cli/mcp_server.py": frozenset({"--reinstall"}),
    "pyfltr/cli/pipeline.py": frozenset({"--fix"}),
    "pyfltr/state/cache.py": frozenset({"--config", "--ignore-path"}),
    "pyfltr/state/retry.py": frozenset({"--foo"}),
}


_OPTION_TOKEN = re.compile(r"(?<!=)--[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*")


_OPTION_LIST_ITEM = re.compile(r"^\s*-\s+`--[A-Za-z]")


_INLINE_CODE = re.compile(r"`([^`\n]+)`")


@functools.lru_cache(maxsize=1)
def _get_options_by_command() -> dict[tuple[str, ...], frozenset[str]]:
    collected: dict[tuple[str, ...], frozenset[str]] = {}

    def walk(parser: argparse.ArgumentParser, command_path: tuple[str, ...]) -> None:
        options: set[str] = set()
        for action in parser._actions:  # pylint: disable=protected-access  # type: ignore[attr-defined]
            options.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):  # pylint: disable=protected-access  # type: ignore[attr-defined]
                for name, subparser in action.choices.items():
                    assert isinstance(subparser, argparse.ArgumentParser)
                    walk(subparser, (*command_path, name))
        collected[command_path] = frozenset(options)

    walk(pyfltr.cli.parser.build_parser(), ())
    return collected


def _get_known_options() -> frozenset[str]:
    return frozenset().union(*_get_options_by_command().values())


def _command_path(value: str) -> tuple[tuple[str, ...], str] | None:
    stripped = value.strip()
    pyfltr_match = re.search(r"\bpyfltr\s+", stripped)
    commandline = stripped[pyfltr_match.end() :] if pyfltr_match else stripped
    command_paths = sorted((path for path in _get_options_by_command() if path), key=len, reverse=True)
    for command_path in command_paths:
        command = " ".join(command_path)
        if commandline == command or commandline.startswith(f"{command} "):
            return command_path, commandline
    if pyfltr_match:
        return (), commandline
    return None


def _unknown_tokens(value: str, known: frozenset[str]) -> set[str]:
    return {token for token in _OPTION_TOKEN.findall(value) if token not in known}


def _unknown_separate_inline_tokens(line: str, code_spans: list[str]) -> tuple[set[str], bool]:
    options_by_command = _get_options_by_command()
    separate_commands: set[tuple[str, ...]] = set()
    separate_option_positions = [line.find(f"`{span}`") for span in code_spans if _OPTION_TOKEN.search(span)]
    for span in code_spans:
        if _OPTION_TOKEN.search(span):
            continue
        command = _command_path(span)
        marker = f"`{span}`"
        marker_start = line.find(marker)
        marker_end = marker_start + len(marker)
        command_precedes_option = any(marker_end < position for position in separate_option_positions)
        if (
            command is not None
            and re.match(r"(?:で|では)", line[marker_end:])
            and (_OPTION_LIST_ITEM.match(line) or command_precedes_option)
        ):
            separate_commands.add(command[0])
    separate_option_spans = [span for span in code_spans if _OPTION_TOKEN.search(span) and _command_path(span) is None]
    if not separate_commands or not separate_option_spans:
        return set(), False
    known = frozenset.intersection(*(options_by_command[path] for path in separate_commands))
    unknown: set[str] = set()
    for span in separate_option_spans:
        unknown.update(_unknown_tokens(span, known))
    return unknown, True


def _unknown_document_tokens(line: str, external_options: frozenset[str]) -> set[str]:
    options_by_command = _get_options_by_command()
    code_spans = _INLINE_CODE.findall(line)
    unknown: set[str] = set()
    has_command_context = False

    for span in code_spans:
        command = _command_path(span)
        if command is None or not _OPTION_TOKEN.search(span):
            continue
        command_path, commandline = command
        unknown.update(_unknown_tokens(commandline, options_by_command[command_path]))
        has_command_context = True

    stripped = line.strip()
    if not code_spans and re.search(r"\bpyfltr\s+", stripped):
        command = _command_path(stripped)
        if command is not None:
            command_path, commandline = command
            unknown.update(_unknown_tokens(commandline, options_by_command[command_path]))
            has_command_context = True

    separate_unknown, has_separate_context = _unknown_separate_inline_tokens(line, code_spans)
    unknown.update(separate_unknown)
    has_command_context = has_command_context or has_separate_context

    if _OPTION_LIST_ITEM.match(line) and not has_command_context:
        unknown.update(_unknown_tokens(line, _get_known_options()) - external_options)
    return unknown


def _split_command_segments(value: str) -> list[str]:
    segments: list[str] = []
    start = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(value):
        if quote == "'":
            if character == "'":
                quote = None
            continue
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if quote == '"':
            if character == '"':
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
            continue
        if character in ";&|":
            segment = value[start:index]
            if segment.strip():
                segments.append(segment)
            start = index + 1
    remainder = value[start:]
    if remainder.strip():
        segments.append(remainder)
    return segments


def _unknown_command_segments(value: str, external_options: frozenset[str]) -> set[str]:
    unknown: set[str] = set()
    for segment in _split_command_segments(value):
        command = _command_path(segment) if re.search(r"\bpyfltr\s+", segment.strip()) else None
        if command is None:
            unknown.update(_unknown_tokens(segment, _get_known_options()) - external_options)
            continue
        command_path, commandline = command
        unknown.update(_unknown_tokens(commandline, _get_options_by_command()[command_path]))
    return unknown


def _strip_string_quotes(literal: str) -> str:
    body = literal.lstrip("bBfFrRuU")
    for quote in ('"""', "'''", '"', "'"):
        if body.startswith(quote) and body.endswith(quote) and len(body) >= 2 * len(quote):
            return body[len(quote) : -len(quote)]
    return body


def _string_literal_value(literal: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            value = ast.literal_eval(literal)
        except (SyntaxError, ValueError):
            return _strip_string_quotes(literal)
    return value if isinstance(value, str) else _strip_string_quotes(literal)


def _python_text_fragments(line: str) -> list[str]:
    # f-stringはPython 3.12以降で複数のトークンへ分解される。
    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    text_types = {getattr(tokenize, name, None) for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")}
    text_types |= {tokenize.STRING, tokenize.COMMENT}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(line).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [line]
    fragments: list[str] = []
    remainder = list(line)
    for token in tokens:
        if token.type not in text_types:
            continue
        if token.type == tokenize.STRING:
            fragments.append(_string_literal_value(token.string))
        elif token.type in {tokenize.COMMENT, fstring_middle}:
            fragments.append(token.string)
        start_row, start_column = token.start
        end_row, end_column = token.end
        if start_row == end_row:
            remainder[start_column:end_column] = " " * (end_column - start_column)
    if not fragments:
        return [line]
    return [*fragments, "".join(remainder)]


def _unknown_python_tokens(line: str, external_options: frozenset[str]) -> set[str]:
    unknown: set[str] = set()
    for fragment in _python_text_fragments(line):
        unknown.update(_unknown_text_tokens(fragment, external_options))
    return unknown


def _unknown_text_tokens(line: str, external_options: frozenset[str]) -> set[str]:
    code_matches = list(_INLINE_CODE.finditer(line))
    code_spans = [match.group(1) for match in code_matches]
    unknown, _has_separate_context = _unknown_separate_inline_tokens(line, code_spans)
    for span in code_spans:
        unknown.update(_unknown_command_segments(span, external_options))

    outside_code = list(line)
    for match in code_matches:
        outside_code[match.start() : match.end()] = " " * (match.end() - match.start())
    unknown.update(_unknown_command_segments("".join(outside_code), external_options))
    return unknown


def _find_unknown_options(text: str, path: pathlib.Path, document: bool) -> list[str]:
    relative_path = path.relative_to(_REPO_ROOT).as_posix()
    external_options = _EXTERNAL_OPTIONS_BY_PATH.get(relative_path, frozenset())
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        unknown = (
            _unknown_document_tokens(line, external_options) if document else _unknown_python_tokens(line, external_options)
        )
        for token in sorted(unknown):
            findings.append(f"{relative_path}:{lineno}: {token}")
    return findings


def test_known_options_are_collected() -> None:
    known = _get_known_options()
    assert "--commands" in known, "既知のはずのオプションを取得できなかった"
    assert "--shuffle" in known, "既知のはずのオプションを取得できなかった"
    assert "--no-shuffle" not in known, "存在しないオプションを既知として取得した"
    options_by_command = _get_options_by_command()
    assert "--global" in options_by_command[("config", "get")]
    assert "--global" not in options_by_command[("run",)]


def test_python_sources_reference_existing_options() -> None:
    findings: list[str] = []
    for zone in _PYTHON_ZONES:
        for path in sorted((_REPO_ROOT / "pyfltr" / zone).rglob("*.py")):
            findings.extend(_find_unknown_options(path.read_text(encoding="utf-8"), path, document=False))
    assert not findings, (
        "実装に定義が無いオプション名を参照している箇所がある:\n"
        + "\n".join(findings)
        + "\n実オプション名へ是正するか、外部ツールの引数であれば _EXTERNAL_OPTIONS_BY_PATH へ追加してください。"
    )


def test_documents_reference_existing_options() -> None:
    findings: list[str] = []
    paths: list[pathlib.Path] = []
    for pattern in _DOCUMENT_GLOBS:
        paths.extend(_REPO_ROOT.glob(pattern))
    for path in sorted(set(paths)):
        findings.extend(_find_unknown_options(path.read_text(encoding="utf-8"), path, document=True))
    assert not findings, (
        "実装に定義が無いオプション名を参照している箇所がある:\n"
        + "\n".join(findings)
        + "\n実オプション名へ是正するか、外部ツールの引数であれば _EXTERNAL_OPTIONS_BY_PATH へ追加してください。"
    )


def test_document_references_use_options_from_the_referenced_command() -> None:
    cases = (
        ("docs/example.md", "本文中の`show-run --tool <name>`を参照する。", "--tool"),
        ("docs/example.md", "- `--global`: `run`で使用する。", "--global"),
        ("docs/example.md", "- `--format`: pyfltrの出力形式を指定する。", "--format"),
        ("docs/example.md", "`pyfltr show-run --global`を案内する。", "--global"),
        ("docs/guide/usage.md", "`pyfltr run --config foo`を案内する。", "--config"),
        ("docs/example.md", "`pyfltr --definitely-invalid`を案内する。", "--definitely-invalid"),
        ("docs/example.md", "`show-run`では`--global`を使用する。", "--global"),
    )
    for relative_path, text, option in cases:
        path = _REPO_ROOT / relative_path
        assert _find_unknown_options(text, path, document=True) == [f"{relative_path}:1: {option}"]


def test_option_values_after_equals_are_not_option_names() -> None:
    path = _REPO_ROOT / "docs/example.md"
    text = "- uvx pyfltr ci --pytest-args=--junitxml=report.xml"
    assert not _find_unknown_options(text, path, document=True)
    text = "- uvx pyfltr ci --definitely-invalid=--junitxml=report.xml"
    assert _find_unknown_options(text, path, document=True) == ["docs/example.md:1: --definitely-invalid"]


def test_python_references_prioritize_pyfltr_command_context() -> None:
    relative_path = "pyfltr/cli/command_info.py"
    path = _REPO_ROOT / relative_path
    assert _find_unknown_options("pyfltr run --format", path, document=False) == [f"{relative_path}:1: --format"]
    assert not _find_unknown_options("textlintへ--formatを渡す。", path, document=False)
    valid_references = (
        "`pyfltr run`では`--shuffle`を使用する。",
        "`pyfltr run`はtextlintへ`--format`を渡す。",
        "pyfltr run --commands=pytest; textlint --format",
        'message = "pyfltr run --commands=pytest; textlint --format"',
        "message = 'pyfltr run --commands=pytest | textlint --format'",
        """message = "pyfltr run --pytest-args='a;b' | textlint --format\"""",
        "# pyfltr run --commands=pytest; textlint --format",
    )
    for reference in valid_references:
        assert not _find_unknown_options(reference, path, document=False)
    for separator in ("&", "&&", "|", "||"):
        reference = f"pyfltr run --commands=pytest {separator} textlint --format"
        assert not _find_unknown_options(reference, path, document=False)
    invalid_references = (
        "pyfltr grep 'a|b' --global",
        'pyfltr grep "a;b" --global',
        r"pyfltr grep a\|b --global",
        "message = \"pyfltr run --pytest-args='a|b' --format\"",
    )
    for reference in invalid_references:
        expected = "--format" if "--format" in reference else "--global"
        assert _find_unknown_options(reference, path, document=False) == [f"{relative_path}:1: {expected}"]


def test_command_segments_respect_quotes_and_escapes() -> None:
    quoted_or_escaped = (
        "pyfltr grep 'a|b' --global",
        'pyfltr grep "a;b" --global',
        r"pyfltr grep a\|b --global",
    )
    for commandline in quoted_or_escaped:
        assert _split_command_segments(commandline) == [commandline]
    assert _split_command_segments("pyfltr run --commands=pytest | textlint --format") == [
        "pyfltr run --commands=pytest ",
        " textlint --format",
    ]


def test_command_segments_without_subcommand_are_not_command_context() -> None:
    # f-stringのリテラル部はPython 3.12以降で独立したトークンとなり、
    # `f"pyfltr {...}"`から`pyfltr `のように末尾空白で終わる断片が生じる。
    for segment in ("pyfltr ", " pyfltr ", "pyfltr"):
        assert _unknown_command_segments(segment, frozenset()) == set()
    assert _unknown_command_segments("pyfltr --unknown-option", frozenset()) == {"--unknown-option"}
    assert _unknown_command_segments("pyfltr --unknown-option", frozenset({"--unknown-option"})) == {"--unknown-option"}
    assert _unknown_command_segments("textlint --unknown-option", frozenset()) == {"--unknown-option"}
    assert _unknown_command_segments("textlint --unknown-option", frozenset({"--unknown-option"})) == set()
    assert _unknown_command_segments("pyfltr run --commands=pytest", frozenset()) == set()
    assert _unknown_text_tokens("pyfltr ", frozenset()) == set()
    assert not _find_unknown_options("pyfltr \n", _REPO_ROOT / "pyfltr" / "cli" / "main.py", document=False)


def test_python_text_fragments_strip_python_quotes() -> None:
    assert _python_text_fragments('m = "pyfltr run --commands=pytest; textlint --format"')[0] == (
        "pyfltr run --commands=pytest; textlint --format"
    )
    assert _python_text_fragments("""m = 'pyfltr grep "a;b" --global'""")[0] == 'pyfltr grep "a;b" --global'
    assert _python_text_fragments(r'm = r"pyfltr grep a\|b --global"')[0] == r"pyfltr grep a\|b --global"
    assert _python_text_fragments('    """pyfltr run --commands=pytest"""')[0] == "pyfltr run --commands=pytest"
    assert _python_text_fragments("value = 1  # pyfltr run --commands=pytest")[0] == "# pyfltr run --commands=pytest"
    assert _python_text_fragments('m = "pyfltr grep --global"') == ["pyfltr grep --global", "m = " + " " * 22]
    assert _python_text_fragments("pyfltr run --commands=pytest") == ["pyfltr run --commands=pytest"]
    assert _python_text_fragments('"""pyfltr run --commands=pytest') == ['"""pyfltr run --commands=pytest']
