import dataclasses
import pathlib
import re

import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_nested
import pyfltr.paths
from pyfltr.diagnostics import ErrorLocation

_PYTEST_CRASH_RE = re.compile(r"worker '(?P<worker>[^']+)' crashed while running '(?P<file>\S.*?)::(?P<test>[^']+)'")


_PYTEST_TB_LINE_RE = re.compile(
    r"^(?P<file>\S.*?):(?P<line>\d+):[ \t]*(?P<message>.+)$",
    re.MULTILINE,
)


def _pytest_error_message(phase: str, test_name: str, message: str, extra: str = "") -> str:
    body = f"{phase} error" if phase else "error"
    if message:
        body = f"{body}: {message}"
    body += extra
    return f"{test_name}: {body}" if test_name else body


_PYTEST_EXCEPTION_LINE_RE = re.compile(
    rf"^E\s+(?P<message>{pyfltr.parsing.pytest_common.PYTEST_EXCEPTION_NAME}: .*|"
    rf"{pyfltr.parsing.pytest_common.PYTEST_EXCEPTION_NAME})$",
    re.MULTILINE,
)


def _pytest_error_body(block: str, error_re: re.Pattern[str], location: re.Match[str] | None) -> str:
    start = location.end() if location is not None else 0
    typed = list(_PYTEST_EXCEPTION_LINE_RE.finditer(block, start)) or list(_PYTEST_EXCEPTION_LINE_RE.finditer(block))
    if typed:
        return typed[-1].group("message").strip()
    error_match = error_re.search(block, start) or error_re.search(block)
    return error_match.group("message").strip() if error_match is not None else ""


def _pick_error_key(
    errors: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]], test_name: str, file_hint: str | None
) -> tuple[str, str] | None:
    candidates = [key for key in errors if key[1] == test_name and key not in consumed]
    for key in candidates:
        if key[0] == file_hint:
            return key
    return candidates[0] if candidates else None


def parse_pytest_errors(
    output: str,
    *,
    errors: dict[tuple[str, str], str | None],
    summary_start: int,
    failures_start: int,
    paths: pyfltr.parsing.pytest_common.PytestPaths,
) -> list[ErrorLocation]:
    consumed: set[tuple[str, str]] = set()
    results: list[ErrorLocation] = []
    errors_start = output.find("= ERRORS =")
    if errors_start >= 0 and not 0 <= failures_start < errors_start:
        section = output[errors_start : summary_start if summary_start > errors_start else len(output)]
        frame_re = re.compile(
            rf"^(?P<file>\S.*?):(?P<line>\d+): (?P<message>in {pyfltr.parsing.pytest_common.PYTEST_FUNCTION_NAME})$",
            re.MULTILINE,
        )
        error_re = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)
        known_names = {test or file for file, test in errors} if errors and summary_start >= 0 else None
        first_captured = pyfltr.parsing.pytest_common.PYTEST_CAPTURED_SECTION_RE.search(section)
        blocks = list(pyfltr.parsing.pytest_common.PYTEST_BLOCK_HEAD_RE.finditer(section))
        for index, block_head in enumerate(blocks):
            block_end = blocks[index + 1].start() if index + 1 < len(blocks) else len(section)
            block = section[block_head.end() : block_end]
            owner = _pytest_error_owner(block_head)
            if owner is None:
                continue
            _, test_name, named_file = owner
            after_captured = first_captured is not None and block_head.start() > first_captured.start()
            if pyfltr.parsing.pytest_nested.is_unlisted_child_block(
                known_names, after_captured=after_captured, test_name=test_name or named_file or ""
            ):
                continue
            location, message, location_file = _pytest_error_location(block, named_file, frame_re, error_re, paths)
            diagnostic = _match_pytest_error(owner, location, location_file, message, errors, consumed, paths)
            if diagnostic is not None:
                results.append(diagnostic)
    results.extend(_remaining_pytest_errors(errors, consumed))
    return results


def _pytest_error_owner(block_head: re.Match[str]) -> tuple[str, str, str | None] | None:
    head = pyfltr.parsing.pytest_common.PYTEST_ERROR_HEAD_RE.fullmatch(block_head.group("test_name").strip())
    if head is None:
        return None
    if head.group("file") is not None:
        phase, test_name = "collection", ""
        named_file: str | None = pyfltr.paths.normalize_separators(head.group("file").strip())
    else:
        phase, test_name = head.group("phase"), head.group("test").strip()
        named_file = None
    return phase, test_name, named_file


def _pytest_error_location(
    block: str,
    named_file: str | None,
    frame_re: re.Pattern[str],
    error_re: re.Pattern[str],
    paths: pyfltr.parsing.pytest_common.PytestPaths,
) -> tuple[re.Match[str] | None, str, str | None]:
    traceback = pyfltr.parsing.pytest_common.pytest_traceback(block)
    native = re.search(r"^Traceback \(most recent call last\):$", traceback, re.MULTILINE)
    if native is not None and named_file is None:
        traceback = traceback[: native.start()]
    frames = list(pyfltr.parsing.pytest_common.pytest_positions(frame_re, traceback, paths))
    location = pyfltr.parsing.pytest_common.select_pytest_location_line(traceback, allow_fallback=not frames, paths=paths)
    if location is None or (frames and location.start() < frames[-1].start()):
        location = next((frame for frame in reversed(frames) if paths.project(frame.group("file"))), None)
        if location is None and frames:
            location = frames[-1]
    message = _pytest_error_body(block, error_re, location)
    location_file = pyfltr.paths.normalize_separators(location.group("file")) if location is not None else None
    return location, message, location_file


def _match_pytest_error(
    owner: tuple[str, str, str | None],
    location: re.Match[str] | None,
    location_file: str | None,
    message: str,
    errors: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
    paths: pyfltr.parsing.pytest_common.PytestPaths,
) -> ErrorLocation | None:
    phase, test_name, named_file = owner
    if named_file is not None:
        key: tuple[str, str] | None = (named_file, "") if (named_file, "") in errors else None
        test_file: str | None = named_file
    else:
        key = _pick_error_key(errors, consumed, test_name, location_file)
        test_file = key[0] if key is not None else None
    if key is not None:
        consumed.add(key)
        message = message or (errors[key] or "")
    if test_file is None:
        if location is None or location_file is None:
            return None
        return ErrorLocation(
            file=location_file,
            line=int(location.group("line")),
            col=None,
            command="pytest",
            message=_pytest_error_message(phase, test_name, message),
        )
    line = 0
    extra = ""
    if location is not None and location_file is not None:
        if location_file == test_file or dataclasses.replace(paths, known_files={test_file}).known(location_file):
            line = int(location.group("line"))
        elif paths.project(location_file):
            extra = f" ({location_file}:{location.group('line')})"
    return ErrorLocation(
        file=test_file, line=line, col=None, command="pytest", message=_pytest_error_message(phase, test_name, message, extra)
    )


def _remaining_pytest_errors(
    errors: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
) -> list[ErrorLocation]:
    results: list[ErrorLocation] = []
    for (file_path, test_name), message in errors.items():
        if (file_path, test_name) in consumed:
            continue
        results.append(
            ErrorLocation(
                file=file_path,
                line=0,
                col=None,
                command="pytest",
                message=_pytest_error_message("" if test_name else "collection", test_name, message or ""),
            )
        )
    return results


def _pytest_failure_diagnostic(
    file_path: str,
    line: int,
    message: str,
    owner: tuple[str, str] | None,
    paths: pyfltr.parsing.pytest_common.PytestPaths,
) -> ErrorLocation:
    if owner is None or file_path == owner[0] or dataclasses.replace(paths, known_files={owner[0]}).known(file_path):
        return ErrorLocation(file=file_path, line=line, col=None, command="pytest", message=message)
    if paths.project(file_path):
        shown = paths.reported_relative(file_path)
        if shown is None and (paths.path_base is not None or paths.start_cwd is not None):
            shown = pyfltr.paths.to_start_relative(
                file_path, path_base=paths.path_base or paths.start_cwd, start_cwd=paths.start_cwd or paths.path_base
            )
        message += f" ({shown or file_path}:{line})"
    return ErrorLocation(file=owner[0], line=0, col=None, command="pytest", message=message)


def parse_pytest_from_summary(
    summary: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]]
) -> list[ErrorLocation]:
    results: list[ErrorLocation] = []
    for (file_path, test_name), message in summary.items():
        if (file_path, test_name) in consumed:
            continue
        raw_message = message or ""
        results.append(
            ErrorLocation(
                file=file_path,
                line=0,
                col=None,
                command="pytest",
                message=f"{test_name}: {raw_message}" if raw_message else test_name,
            )
        )
    return results


def parse_pytest(
    output: str, *, path_base: pathlib.Path | None = None, start_cwd: pathlib.Path | None = None
) -> list[ErrorLocation]:
    prepared = _prepare_pytest_input(output, path_base, start_cwd)
    consumed: set[tuple[str, str]] = set()
    if prepared.failures_start < 0:
        return [*parse_pytest_from_summary(prepared.summary, consumed), *prepared.error_results]
    end = prepared.summary_start if prepared.summary_start > prepared.failures_start else len(prepared.output)
    section = prepared.output[prepared.failures_start : end]
    blocks = list(pyfltr.parsing.pytest_common.PYTEST_BLOCK_HEAD_RE.finditer(section))
    if blocks:
        results = _parse_pytest_failure_blocks(
            section, blocks, prepared.summary, consumed, prepared.paths, prepared.summary_start
        )
    else:
        results = _parse_pytest_lines(section, prepared.summary, consumed, prepared.paths)
    results.extend(parse_pytest_from_summary(prepared.summary, consumed))
    results.extend(prepared.error_results)
    return results


@dataclasses.dataclass
class _PytestInput:
    output: str
    paths: pyfltr.parsing.pytest_common.PytestPaths
    failures_start: int
    summary_start: int
    summary: dict[tuple[str, str], str | None]
    error_results: list[ErrorLocation]


def _prepare_pytest_input(output: str, path_base: pathlib.Path | None, start_cwd: pathlib.Path | None) -> _PytestInput:
    paths = pyfltr.parsing.pytest_common.pytest_paths(output, path_base, start_cwd)
    output = pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs(output, paths=paths)

    failures_start = output.find("= FAILURES =")
    summary_start = pyfltr.parsing.pytest_nested.pytest_parent_summary_start(output)

    if summary_start >= 0:
        summary = pyfltr.parsing.pytest_common.parse_pytest_summary(output[summary_start:])
        errors = pyfltr.parsing.pytest_common.parse_pytest_error_summary(output[summary_start:])
    elif pyfltr.parsing.pytest_nested.has_pytest_summary_head(output):
        summary = {}
        errors = {}
    else:
        summary = pyfltr.parsing.pytest_common.parse_pytest_summary(output)
        errors = {}
    paths.known_files = pyfltr.parsing.pytest_common.pytest_paths(output).known_files | {file for file, _ in errors}
    error_results = parse_pytest_errors(
        output, errors=errors, summary_start=summary_start, failures_start=failures_start, paths=paths
    )

    return _PytestInput(output, paths, failures_start, summary_start, summary, error_results)


def _parse_pytest_lines(
    failures_section: str,
    summary: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
    paths: pyfltr.parsing.pytest_common.PytestPaths,
) -> list[ErrorLocation]:
    results: list[ErrorLocation] = []
    line_paths = dataclasses.replace(paths, allow_expanded=True)
    captured = False
    previous_end = 0
    for match in pyfltr.parsing.pytest_common.pytest_positions(_PYTEST_TB_LINE_RE, failures_section, line_paths):
        if (
            pyfltr.parsing.pytest_common.PYTEST_CAPTURED_SECTION_RE.search(failures_section[previous_end : match.start()])
            is not None
        ):
            captured = True
        previous_end = match.end()
        file_path = pyfltr.paths.normalize_separators(match.group("file"))
        if not paths.project(file_path):
            continue
        message = match.group("message").strip()
        matched_key: tuple[str, str] | None = None
        doctest_location = message in ("DocTestFailure", "UnexpectedException")
        if doctest_location:
            matched_key = next(
                (key for key in summary if key not in consumed and (file_path == key[0] or file_path.endswith("/" + key[0]))),
                None,
            )
        for key, sum_message in summary.items():
            if matched_key is not None:
                break
            if key in consumed:
                continue
            if key[0] == file_path and sum_message == message:
                matched_key = key
                break
        if matched_key is None:
            for key, sum_message in summary.items():
                if key in consumed:
                    continue
                if sum_message == message:
                    matched_key = key
                    break
        if matched_key is None:
            matched_key = pyfltr.parsing.pytest_common.match_truncated_summary(summary, consumed, message)
        if pyfltr.parsing.pytest_common.PYTEST_LEGACY_FILE_RE.fullmatch(file_path) is None:
            if doctest_location:
                continue
            if matched_key is None or not (
                pathlib.PurePosixPath(file_path).is_absolute() or pathlib.PureWindowsPath(file_path).is_absolute()
            ):
                continue
            owner = dataclasses.replace(paths, known_files={matched_key[0]})
            if not owner.known(file_path):
                continue
            if captured and (
                matched_key != next((key for key in summary if key not in consumed), None)
                or summary.get(matched_key) != message
            ):
                continue
        captured = False
        test_name = matched_key[1] if matched_key is not None else None
        if matched_key is not None:
            consumed.add(matched_key)
        results.append(
            _pytest_failure_diagnostic(
                file_path,
                int(match.group("line")),
                test_name if doctest_location and test_name else f"{test_name}: {message}" if test_name else message,
                None if doctest_location else matched_key,
                paths,
            )
        )
    return results


def _parse_pytest_failure_blocks(
    failures_section: str,
    block_matches: list[re.Match[str]],
    summary: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
    paths: pyfltr.parsing.pytest_common.PytestPaths,
    summary_start: int,
) -> list[ErrorLocation]:
    frame_re = re.compile(
        rf"^(?P<file>\S.*?):(?P<line>\d+): (?P<message>in {pyfltr.parsing.pytest_common.PYTEST_FUNCTION_NAME})$", re.MULTILINE
    )
    error_re = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)

    known_tests = {test for _, test in summary} if summary and summary_start >= 0 else None
    first_captured = pyfltr.parsing.pytest_common.PYTEST_CAPTURED_SECTION_RE.search(failures_section)

    results = []
    for i, match in enumerate(block_matches):
        start = match.end()
        block_end = block_matches[i + 1].start() if i + 1 < len(block_matches) else len(failures_section)
        block = failures_section[start:block_end]
        if pyfltr.parsing.pytest_common.PYTEST_ERROR_HEAD_RE.fullmatch(match.group("test_name").strip()) is not None:
            continue
        test_name = match.group("test_name").strip().removeprefix("[doctest] ")
        after_captured = first_captured is not None and match.start() > first_captured.start()

        traceback = pyfltr.parsing.pytest_common.pytest_traceback(block)
        block_paths = pyfltr.parsing.pytest_common.pytest_block_paths(
            paths, summary, test_name, traceback, doctest=match["test_name"].startswith("[doctest] ")
        )
        if match["test_name"].startswith("[doctest] "):
            terminal = [
                item
                for item in pyfltr.parsing.pytest_common.pytest_positions(
                    pyfltr.parsing.pytest_common.PYTEST_LOCATION_LINE_RE, traceback, block_paths
                )
                if item["message"].strip() in ("DocTestFailure", "UnexpectedException")
            ]
            if terminal:
                location = terminal[-1]
                file_path = pyfltr.paths.normalize_separators(location["file"])
                results.append(
                    ErrorLocation(file=file_path, line=int(location["line"]), col=None, command="pytest", message=test_name)
                )
                pyfltr.parsing.pytest_common.consume_summary_test(summary, consumed, test_name, file_path)
                continue
        native = re.search(r"^Traceback \(most recent call last\):$", traceback, re.MULTILINE)
        if native is not None:
            traceback = traceback[: native.start()]
        frames = list(pyfltr.parsing.pytest_common.pytest_positions(frame_re, traceback, block_paths))
        location = pyfltr.parsing.pytest_common.select_pytest_location_line(
            traceback, allow_fallback=not frames, paths=block_paths
        )
        entry = next(
            pyfltr.parsing.pytest_common.pytest_positions(
                pyfltr.parsing.pytest_common.PYTEST_LOCATION_LINE_RE, traceback, block_paths
            ),
            None,
        )
        owner_hint = pyfltr.paths.normalize_separators(entry["file"]) if entry is not None else None
        if location is not None and (not frames or location.start() > frames[-1].start()):
            if pyfltr.parsing.pytest_nested.is_unlisted_child_block(
                known_tests, after_captured=after_captured, test_name=test_name
            ):
                continue
            file_path = pyfltr.paths.normalize_separators(location.group("file"))
            raw_message = pyfltr.parsing.pytest_common.pytest_block_message(block, error_re, location, block_paths)
            owner = pyfltr.parsing.pytest_common.consume_summary_test(summary, consumed, test_name, owner_hint or file_path)
            results.append(
                _pytest_failure_diagnostic(
                    file_path,
                    int(location.group("line")),
                    pyfltr.parsing.pytest_common.pytest_join_message(test_name, raw_message),
                    owner,
                    paths,
                )
            )
            continue

        if frames:
            if pyfltr.parsing.pytest_nested.is_unlisted_child_block(
                known_tests, after_captured=after_captured, test_name=test_name
            ):
                continue
            chosen = frames[-1]
            for frame in reversed(frames):
                if paths.project(frame.group("file")):
                    chosen = frame
                    break
            error_match = error_re.search(block, chosen.end()) or error_re.search(block)
            raw_message = error_match.group("message").strip() if error_match else ""
            message = pyfltr.parsing.pytest_common.pytest_join_message(test_name, raw_message)
            file_path = pyfltr.paths.normalize_separators(chosen.group("file"))
            owner = pyfltr.parsing.pytest_common.consume_summary_test(summary, consumed, test_name, owner_hint or file_path)
            results.append(_pytest_failure_diagnostic(file_path, int(chosen.group("line")), message, owner, paths))
            continue

        crash = _parse_pytest_crash(block, test_name, known_tests, after_captured, summary, consumed, paths)
        if crash is not None:
            results.append(crash)
    return results


def _parse_pytest_crash(
    block: str,
    test_name: str,
    known_tests: set[str] | None,
    after_captured: bool,
    summary: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
    paths: pyfltr.parsing.pytest_common.PytestPaths,
) -> ErrorLocation | None:
    crash_match = _PYTEST_CRASH_RE.search(block)
    if crash_match is None:
        return None
    file_path = pyfltr.paths.normalize_separators(crash_match.group("file"))
    crashed_test = crash_match.group("test")
    if pyfltr.parsing.pytest_common.PYTEST_LEGACY_FILE_RE.fullmatch(file_path) is None:
        crashed_name = crashed_test.replace("::", ".")
        block_name = pyfltr.paths.normalize_separators(test_name)
        if block_name not in (file_path, crashed_name):
            return None
        owners = {file for file, test in summary if test == crashed_name}
        if block_name == file_path:
            owners.add(file_path)
        if not dataclasses.replace(paths, known_files=owners).known(file_path):
            return None
    if pyfltr.parsing.pytest_nested.is_unlisted_child_block(
        known_tests, after_captured=after_captured, test_name=crashed_test.replace("::", ".")
    ):
        return None
    result = ErrorLocation(
        file=file_path,
        line=0,
        col=None,
        command="pytest",
        message=(f"{crashed_test}: worker '{crash_match.group('worker')}' crashed while running {file_path}::{crashed_test}"),
    )
    pyfltr.parsing.pytest_common.consume_summary_test(summary, consumed, crashed_test.replace("::", "."), file_path)

    return result


def summarize_pytest(output: str) -> str | None:
    match = re.search(r"=+ (.+?) =+\s*$", output)
    if match is None:
        return None
    return match.group(1)
