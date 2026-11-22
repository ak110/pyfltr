import re

import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_nested

PYTEST_CONFIG_CONFLICT_RE = re.compile(
    r"^configfile: (?P<used>.+?) \(WARNING: ignoring pytest config in (?P<ignored>.+?)!\)\s*$"
)


PYTEST_COLLECTION_RE = re.compile(r"^(?:collecting |collected \d+ items?)")


PYTEST_SECTION_RE = re.compile(r"^=+ .+ =+$")


def is_pytest_header_end(line: str) -> bool:
    return (
        PYTEST_COLLECTION_RE.match(line) is not None
        or PYTEST_SECTION_RE.fullmatch(line) is not None
        or pyfltr.parsing.pytest_nested.is_pytest_nested_run_marker(line)
    )


def detect_pytest_config_conflict(output: str) -> str | None:
    output = pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs(output)
    lines = [pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", line).strip() for line in output.splitlines()]
    header_start: int | None = None
    for index, line in enumerate(lines):
        if pyfltr.parsing.pytest_nested.PYTEST_SESSION_START_RE.fullmatch(line):
            header_start = index
            break
        if is_pytest_header_end(line):
            return None
    if header_start is None:
        return None
    previous_line = ""
    for line in lines[header_start + 1 :]:
        if is_pytest_header_end(line):
            return None
        match = PYTEST_CONFIG_CONFLICT_RE.match(line)
        if match is None:
            previous_line = line
            continue
        if not previous_line.startswith("rootdir: "):
            previous_line = line
            continue
        used = match.group("used")
        ignored = match.group("ignored")
        return f"pytest: 設定ファイルが競合しています。採用: {used} / 無視: {ignored}"
    return None
