import re

import pyfltr.parsing.pytest_common
import pyfltr.paths

PYTEST_SESSION_START_RE = re.compile(r"^=+ test session starts =+$")


PYTEST_SESSION_END_RE = re.compile(r"^=+ .* in \d+(?:\.\d+)?s.*=+$")


PYTEST_QUIET_SESSION_END_RE = re.compile(
    r"^(?:\d+ \w+(?:, \d+ \w+)*|no tests ran) in \d+(?:\.\d+)?s(?: \((?:\d+ days?, )?\d+:\d{2}:\d{2}\))?$"
)


PYTEST_SUMMARY_HEAD_RE = re.compile(r"^=+ short test summary info =+$")


PYTEST_NESTED_RUN_MARKER_RES = (
    pyfltr.parsing.pytest_common.PYTEST_BLOCK_HEAD_RE,
    pyfltr.parsing.pytest_common.PYTEST_CAPTURED_SECTION_RE,
    PYTEST_SESSION_START_RE,
    PYTEST_SESSION_END_RE,
    PYTEST_QUIET_SESSION_END_RE,
    PYTEST_SUMMARY_HEAD_RE,
)


def pytest_parent_summary_start(output: str) -> int:
    lines = output.split("\n")
    limit = pytest_parent_tail_index(lines)
    for following in range(limit + 1, len(lines)):
        if PYTEST_SESSION_START_RE.fullmatch(lines[following].rstrip("\r")):
            break
        if is_pytest_nested_run_marker(lines[following]):
            limit = len(lines)
            break
    heads = [index for index, line in enumerate(lines[:limit]) if PYTEST_SUMMARY_HEAD_RE.fullmatch(line.rstrip())]
    for index in reversed(heads):
        if any(is_pytest_nested_run_marker(lines[following]) for following in range(index + 1, limit)):
            continue
        return sum(len(line) + 1 for line in lines[:index])
    return -1


def is_pytest_nested_run_marker(line: str) -> bool:
    return any(pattern.fullmatch(line.rstrip("\r")) for pattern in PYTEST_NESTED_RUN_MARKER_RES)


def has_pytest_summary_head(output: str) -> bool:
    return any(PYTEST_SUMMARY_HEAD_RE.fullmatch(line.rstrip()) for line in output.split("\n"))


def pytest_child_run_ranges(
    output: str, *, paths: pyfltr.parsing.pytest_common.PytestPaths | None = None
) -> list[tuple[int, int]]:
    paths = paths or pyfltr.parsing.pytest_common.pytest_paths(output)
    lines = output.split("\n")
    limit = pytest_parent_tail_index(lines)
    for following in lines[limit + 1 :]:
        if re.search(r"=+ test session starts =+$", following.rstrip()):
            break
        if is_pytest_nested_run_marker(following):
            limit = len(lines)
            break
    parent_header_index = -1
    if limit < len(lines) and PYTEST_SESSION_END_RE.fullmatch(
        pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", lines[limit]).rstrip()
    ):
        first = next(
            (
                index
                for index, line in enumerate(lines)
                if re.fullmatch(r"=+ test session starts =+", pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", line).strip())
            ),
            -1,
        )
        if first >= 0 and not any(
            _is_pytest_result_line(pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", line).rstrip()) for line in lines[:first]
        ):
            depth_stack: list[int] = []
            for index, line in enumerate(lines[: limit + 1]):
                clean = pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", line).rstrip()
                if re.search(r"=+ test session starts =+$", clean):
                    depth_stack.append(index)
                elif PYTEST_SESSION_END_RE.fullmatch(clean) and depth_stack and depth_stack.pop() == first:
                    if index == limit:
                        parent_header_index = first
                    break
    stack: list[int] = []
    ranges: list[tuple[int, int]] = []
    summary_start = pytest_parent_summary_start(output)
    parent_tests = summary_tests(output[summary_start:]) if summary_start >= 0 else set()
    for index, line in enumerate(lines):
        clean = pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", line).rstrip()
        if re.search(r"=+ test session starts =+$", clean):
            if index != parent_header_index:
                stack.append(index)
        elif (PYTEST_SESSION_END_RE.fullmatch(clean) or PYTEST_QUIET_SESSION_END_RE.fullmatch(clean)) and stack:
            start = stack.pop()
            if index < limit:
                candidate = "\n".join(lines[start : index + 1])
                parent_files = {file for file, _ in parent_tests}
                if not parent_files:
                    prefix = "\n".join(lines[:start])
                    failures_start = prefix.find("= FAILURES =")
                    if failures_start >= 0:
                        parent_files = {
                            pyfltr.paths.normalize_separators(location["file"])
                            for location in pyfltr.parsing.pytest_common.pytest_positions(
                                pyfltr.parsing.pytest_common.PYTEST_LOCATION_LINE_RE, prefix[failures_start:], paths
                            )
                        }
                if not crosses_pytest_parent_blocks(candidate, parent_tests, parent_files, paths):
                    ranges.append((start, index))
    return ranges


def _is_pytest_result_line(line: str) -> bool:
    return bool(
        PYTEST_SESSION_END_RE.fullmatch(line)
        or PYTEST_QUIET_SESSION_END_RE.fullmatch(line)
        or re.fullmatch(r"=+ [A-Za-z ]+ =+", line)
        or re.match(r"^\S+\.py [.FEsxX]+", line)
        or is_pytest_nested_run_marker(line)
    )


def summary_tests(output: str) -> set[tuple[str, str]]:
    return set(pyfltr.parsing.pytest_common.parse_pytest_summary(output)) | {
        key for key in pyfltr.parsing.pytest_common.parse_pytest_error_summary(output) if key[1]
    }


def crosses_pytest_parent_blocks(
    candidate: str, parent_tests: set[tuple[str, str]], parent_files: set[str], paths: pyfltr.parsing.pytest_common.PytestPaths
) -> bool:
    summary_start = pytest_parent_summary_start(candidate)
    child_tests = summary_tests(candidate[summary_start:] if summary_start >= 0 else candidate)
    blocks = list(pyfltr.parsing.pytest_common.PYTEST_BLOCK_HEAD_RE.finditer(candidate))
    for index, block in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(candidate)
        test_name = block["test_name"].strip().removeprefix("[doctest] ")
        error_head = pyfltr.parsing.pytest_common.PYTEST_ERROR_HEAD_RE.fullmatch(test_name)
        if error_head is not None and error_head.group("test") is not None:
            test_name = error_head.group("test").strip()
            parent_names = {test for _, test in parent_tests}
            if test_name in parent_names and all(test != test_name for _, test in child_tests):
                return True
        for location in pyfltr.parsing.pytest_common.pytest_positions(
            pyfltr.parsing.pytest_common.PYTEST_LOCATION_LINE_RE,
            pyfltr.parsing.pytest_common.pytest_traceback(candidate[block.end() : end]),
            paths,
        ):
            key = (pyfltr.paths.normalize_separators(location["file"]), test_name)
            if (key in parent_tests or key[0] in parent_files) and key not in child_tests:
                return True
    return False


def mask_pytest_captured_child_runs(output: str, *, paths: pyfltr.parsing.pytest_common.PytestPaths | None = None) -> str:
    lines = output.split("\n")
    for start, end in pytest_child_run_ranges(output, paths=paths):
        lines[start : end + 1] = [""] * (end - start + 1)
    return "\n".join(lines)


def pytest_parent_tail_index(lines: list[str]) -> int:
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].rstrip()
        if PYTEST_SESSION_END_RE.fullmatch(stripped) or PYTEST_QUIET_SESSION_END_RE.fullmatch(stripped):
            return i
    return len(lines)


def is_unlisted_child_block(known_tests: set[str] | None, *, after_captured: bool, test_name: str) -> bool:
    return known_tests is not None and after_captured and test_name not in known_tests
