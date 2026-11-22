import collections.abc
import dataclasses
import pathlib
import re

import pyfltr.paths
from pyfltr.parsing.common import is_project_path

PYTEST_SUMMARY_RE = re.compile(
    r"^FAILED\s+(?P<file>\S.*?)::(?P<test>[^\s\[]+(?:\[.*?\])?)(?:\s+-\s+(?P<message>.+))?$",
    re.MULTILINE,
)


PYTEST_ERROR_SUMMARY_RE = re.compile(
    r"^ERROR\s+(?P<file>\S.*?)(?:::(?P<test>[^\s\[]+(?:\[.*?\])?))?(?:\s+-\s+(?P<message>.+))?$",
    re.MULTILINE,
)


PYTEST_ERROR_HEAD_RE = re.compile(r"ERROR at (?P<phase>setup|teardown) of (?P<test>.+)|ERROR collecting (?P<file>.+)")


PYTEST_BLOCK_HEAD_RE = re.compile(r"^_+ (?P<test_name>.*?[^\s_].*?) _+$", re.MULTILINE)


PYTEST_CAPTURED_SECTION_RE = re.compile(r"^-+ Captured .+ -+$", re.MULTILINE)


PYTEST_FUNCTION_NAME = r"(?:[\w.]+|<[\w ]+>)"


PYTEST_EXCEPTION_NAME = r"[^\W\d][\w.]*"


PYTEST_LEGACY_FILE_RE = re.compile(r"(?:[A-Za-z]:)?[^\s:]+")


PYTEST_LOCATION_LINE_RE = re.compile(
    rf"^(?P<file>\S.*?):(?P<line>\d+):"
    rf"(?P<message>[ \t]*(?:in {PYTEST_FUNCTION_NAME}|{PYTEST_EXCEPTION_NAME})?)[ \t]*$",
    re.MULTILINE,
)


@dataclasses.dataclass
class PytestPaths:
    known_files: set[str]
    path_base: pathlib.Path | None = None
    start_cwd: pathlib.Path | None = None
    reported_root: str | None = None
    allow_expanded: bool = False

    def reported_relative(self, file: str) -> str | None:
        if self.reported_root is None:
            return None
        try:
            return str(pathlib.PurePosixPath(file).relative_to(self.reported_root))
        except ValueError:
            return None

    def known(self, file: str) -> bool:
        if file in self.known_files:
            return True
        relative = self.reported_relative(file)
        if relative is not None and relative in self.known_files:
            return True
        if self.path_base is not None or self.start_cwd is not None:
            relative = pyfltr.paths.to_start_relative(
                file, path_base=self.path_base or self.start_cwd, start_cwd=self.start_cwd or self.path_base
            )
            return relative in self.known_files
        return False

    def exists(self, file: str) -> bool:
        bases = [base for base in (self.path_base, self.start_cwd) if base is not None]
        if not bases:
            return False
        names = [file]
        if (relative := self.reported_relative(file)) is not None:
            names.append(relative)
        try:
            return any((base / name).is_file() for base in bases for name in names)
        except (OSError, ValueError):
            return False

    def accepts(self, file: str) -> bool:
        file = pyfltr.paths.normalize_separators(file)
        if file.startswith("<"):
            return False
        return PYTEST_LEGACY_FILE_RE.fullmatch(file) is not None or (self.allow_expanded and self.known(file))

    def project(self, file: str) -> bool:
        file = pyfltr.paths.normalize_separators(file)
        if self.known(file):
            return True
        if (relative := self.reported_relative(file)) is not None:
            return is_project_path(relative)
        if self.path_base is not None or self.start_cwd is not None:
            file = pyfltr.paths.to_start_relative(
                file, path_base=self.path_base or self.start_cwd, start_cwd=self.start_cwd or self.path_base
            )
        if pathlib.PureWindowsPath(file).is_absolute():
            return False
        return is_project_path(file)


def pytest_paths(output: str, path_base: pathlib.Path | None = None, start_cwd: pathlib.Path | None = None) -> PytestPaths:
    root = re.search(r"^rootdir: (.+)$", output, re.MULTILINE)
    return PytestPaths(
        {pyfltr.paths.normalize_separators(file) for file, _ in parse_pytest_summary(output)}
        | {
            pyfltr.paths.normalize_separators(match[1])
            for match in re.finditer(r"^_+ \[doctest\] (.+?) _+$", output, re.MULTILINE)
        }
        | {
            pyfltr.paths.normalize_separators(match[1].split("::", 1)[0])
            for match in re.finditer(r"^(.+?) +[.FEsxX]+ *(?:\[ *\d+%\])?$", output, re.MULTILINE)
        },
        path_base,
        start_cwd,
        pyfltr.paths.normalize_separators(root[1]) if root is not None else None,
    )


def pytest_positions(
    pattern: re.Pattern[str], output: str, paths: PytestPaths | None = None
) -> collections.abc.Iterator[re.Match[str]]:
    paths = paths or pytest_paths(output)
    captured = False
    for line in re.finditer(r"^\S[^\n]*", output, re.MULTILINE):
        if PYTEST_CAPTURED_SECTION_RE.fullmatch(line[0]):
            captured = True
            continue
        if re.match(r"E\s", line[0]) is not None:
            captured = False
        for separator in reversed(list(re.finditer(r":\d+:", line[0]))):
            file = line[0][: separator.start()]
            if PYTEST_LEGACY_FILE_RE.fullmatch(file) is None and file.startswith(("E ", "E\t", ">")):
                continue
            if not paths.accepts(file):
                continue
            if captured and not (paths.known(file) or paths.exists(file) or "." in file or "/" in file or "\\" in file):
                continue
            exact = pattern.pattern.replace(r"(?P<file>\S.*?)", rf"(?P<file>{re.escape(file)})", 1)
            match = re.compile(exact, pattern.flags).match(output, line.start())
            if match is not None:
                captured = False
                yield match
                break


def pytest_block_paths(
    paths: PytestPaths, summary: dict[tuple[str, str], str | None], test_name: str, block: str, *, doctest: bool
) -> PytestPaths:
    owned = {file for file, test in summary if test == test_name}
    if doctest:
        return dataclasses.replace(paths, known_files=owned | {test_name}, allow_expanded=True)
    function = test_name.split("[", 1)[0].rsplit(".", 1)[-1]
    trace = block.lstrip("\r\n")
    trace = re.sub(r"^\[gw[^\]]+\] [^\n]*\n", "", trace, count=1).lstrip("\r\n")
    error = re.search(r"^E[ \t]+", trace, re.MULTILINE)
    if error is None:
        return dataclasses.replace(paths, known_files=owned, allow_expanded=False)
    prefix = trace[: error.start()]
    frame = re.match(rf"(?P<file>\S.*):\d+: in {re.escape(function)}\n[ \t]+\S", prefix)
    source = re.search(rf"^ +(?:async )?def {re.escape(function)}\s*\(", prefix, re.MULTILINE)
    if source is not None and (
        any(line.strip() and re.match(r"^\w+ = ", line) is None for line in prefix[: source.start()].splitlines())
        or re.search(r"^>", prefix[source.end() :], re.MULTILINE) is None
    ):
        source = None
    if frame is not None and not owned:
        owned.add(pyfltr.paths.normalize_separators(frame["file"]))
    if source is not None and not owned:
        first = next(
            (
                item
                for item in PYTEST_LOCATION_LINE_RE.finditer(trace, source.end())
                if not item["file"].startswith(("E ", "E\t", ">"))
            ),
            None,
        )
        if first is not None:
            owned.add(pyfltr.paths.normalize_separators(first["file"]))
    return dataclasses.replace(paths, known_files=owned, allow_expanded=bool(frame or source))


def pytest_traceback(block: str) -> str:
    section = PYTEST_CAPTURED_SECTION_RE.search(block)
    return block[: section.start()] if section is not None else block


ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_pytest_summary(output: str) -> dict[tuple[str, str], str | None]:
    summary: dict[tuple[str, str], str | None] = {}
    for match in PYTEST_SUMMARY_RE.finditer(output):
        file_path = pyfltr.paths.normalize_separators(match.group("file"))
        test_name = match.group("test").replace("::", ".")
        raw_message = match.group("message")
        summary[(file_path, test_name)] = raw_message.strip() if raw_message is not None else None
    return summary


def parse_pytest_error_summary(output: str) -> dict[tuple[str, str], str | None]:
    errors: dict[tuple[str, str], str | None] = {}
    for match in PYTEST_ERROR_SUMMARY_RE.finditer(output):
        file_path = pyfltr.paths.normalize_separators(match.group("file"))
        test_name = (match.group("test") or "").replace("::", ".")
        raw_message = match.group("message")
        errors[(file_path, test_name)] = raw_message.strip() if raw_message is not None else None
    return errors


def consume_summary_test(
    summary: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
    test_name: str,
    diagnosed_file: str,
) -> tuple[str, str] | None:
    fallback_key: tuple[str, str] | None = None
    for key in summary:
        sum_file, sum_test = key
        if sum_test != test_name or key in consumed:
            continue
        if sum_file == diagnosed_file:
            consumed.add(key)
            return key
        if fallback_key is None:
            fallback_key = key
    if fallback_key is not None:
        consumed.add(fallback_key)
    return fallback_key


def match_truncated_summary(
    summary: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]], message: str
) -> tuple[str, str] | None:
    matched = [
        key
        for key, sum_message in summary.items()
        if key not in consumed
        and sum_message is not None
        and sum_message.endswith("...")
        and message.startswith(sum_message[: -len("...")])
    ]
    return matched[0] if len(matched) == 1 else None


def select_pytest_location_line(block: str, *, allow_fallback: bool, paths: PytestPaths | None = None) -> re.Match[str] | None:
    paths = paths or pytest_paths(block)
    scan_target = pytest_traceback(block)
    typed: list[re.Match[str]] = []
    bare: list[re.Match[str]] = []
    for match in pytest_positions(PYTEST_LOCATION_LINE_RE, scan_target, paths):
        (typed if match.group("message").strip() else bare).append(match)
    for candidates in (typed, bare) if allow_fallback else (typed,):
        for match in reversed(candidates):
            if paths.project(match.group("file")):
                return match
    return typed[-1] if typed and allow_fallback else None


def pytest_block_message(
    block: str, error_re: re.Pattern[str], location: re.Match[str], paths: PytestPaths | None = None
) -> str:
    previous_end = 0
    for match in pytest_positions(PYTEST_LOCATION_LINE_RE, block[: location.start()], paths):
        previous_end = match.end()
    error_match = error_re.search(block, previous_end) or error_re.search(block)
    return error_match.group("message").strip() if error_match is not None else ""


def pytest_join_message(test_name: str, raw_message: str) -> str:
    if not test_name:
        return raw_message
    return f"{test_name}: {raw_message}" if raw_message else test_name
