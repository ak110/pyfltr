"""pytestの親子出力と診断の解析。"""

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
    """ERRORの診断メッセージを組み立てる。段階を併記し、同じ試験の本体の失敗と区別する。

    一覧だけから得た項目は段階（setup・teardown）を判別できないため、`phase`を空文字列とする。
    """
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
    """ERRORブロックの理由を返す。例外名で始まる最後の`E`行を優先する。

    収集時の`SyntaxError`などは`E`行の先頭に`File "...", line N`のような補足を出し、
    例外名と本文は後続の`E`行に現れる。
    """
    start = location.end() if location is not None else 0
    typed = list(_PYTEST_EXCEPTION_LINE_RE.finditer(block, start)) or list(_PYTEST_EXCEPTION_LINE_RE.finditer(block))
    if typed:
        return typed[-1].group("message").strip()
    error_match = error_re.search(block, start) or error_re.search(block)
    return error_match.group("message").strip() if error_match is not None else ""


def _pick_error_key(
    errors: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]], test_name: str, file_hint: str | None
) -> tuple[str, str] | None:
    """ERRORS節のブロックに対応する一覧の項目を選ぶ。位置のファイルと一致する候補を優先する。"""
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
    """試験の準備・後始末・収集のERRORを診断化する。

    ERRORS節（`= ERRORS =`）のブロックから理由と位置を取り、一覧（`errors`）の項目と突合する。
    一覧で試験ファイルが判明する場合は診断の`file`を試験ファイルとする。
    共有fixtureのように例外位置が試験ファイルの外にあると、位置のファイルは実行対象との交差で
    `retry_command`から消えるため、例外位置は行番号付きでメッセージへ併記し、行番号は`0`とする。
    一覧が無い構成（`-rN`等）では、ERRORS節のトレースバックの位置（プロジェクト内を優先）を使う。
    ブロックと突合できなかった一覧の項目は`line=0`の診断として補完する。

    捕捉出力へ混入した子pytestのERRORS節は`pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs`で除かれる。
    除外が成立しない構成に備え、親の一覧に載らない捕捉出力後のブロックを除く安全網を
    `pyfltr.parsing.pytest_nested.is_unlisted_child_block`で併用する（一覧が空の構成では働かせない）。
    """
    consumed: set[tuple[str, str]] = set()
    results: list[ErrorLocation] = []
    errors_start = output.find("= ERRORS =")
    # pytestはERRORS節を失敗欄より前に出力する。失敗欄より後にある節は失敗欄の捕捉出力へ混入した子pytestのものである。
    if errors_start >= 0 and not 0 <= failures_start < errors_start:
        # 失敗欄の見出しで区切らず失敗一覧の手前まで走査する。親のERRORブロックの捕捉出力に子pytestの失敗欄が
        # 混入すると最初の失敗欄の見出しは子のものになり、そこで区切ると後続の親のERRORブロックを失うためである。
        # ERRORの見出しでないブロック（親子の失敗欄）は下の判定で対象から外す。
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
    """ERRORブロックのphase・試験名・収集対象ファイルを判別する。"""
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
    """ERRORのtracebackから位置と理由を選ぶ。"""
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
    """ERRORブロックを一覧へ対応付け、診断のファイルと補足位置を確定する。"""
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
            # 共有fixture等のプロジェクト内の例外位置だけを併記し、外部の位置は修正先にしない。
            extra = f" ({location_file}:{location.group('line')})"
    return ErrorLocation(
        file=test_file, line=line, col=None, command="pytest", message=_pytest_error_message(phase, test_name, message, extra)
    )


def _remaining_pytest_errors(
    errors: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
) -> list[ErrorLocation]:
    """ブロックで消費されなかったERROR一覧を補完する。"""
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


def parse_pytest_from_summary(
    summary: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]]
) -> list[ErrorLocation]:
    """summary辞書のうち他経路で拾えなかった残余エントリを`line=0`の診断として補完する。

    `consumed`はsummary辞書のキーそのもの（`(file, test)`の組）の集合である。
    キーの決定は`pyfltr.parsing.pytest_common.consume_summary_test`が担い、フレーム選択がプロジェクト外フレーム
    （`.venv/`配下等）へフォールバックし診断の`file`とsummaryの`file`が一致しない場合でも、
    同名テストの未消費候補へフォールバックして消費するため、ファイルパス込みキーでも
    二重生成を招かない。同名テストが別ファイルに存在する場合はファイル込みキーにより
    それぞれ独立したエントリとして扱われ、取りこぼしを防ぐ。
    """
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
    """Pytest出力をパース。

    次の優先順で情報源を扱い、いずれの経路でも失敗理由の本文を可能な限り保持する。

    1. `short test summary info`の`FAILED <file>::<test> - <message>`行を
       `(file, test) -> message`辞書として先に収集する（メッセージ補完・突合用）。
       `test`は`.`区切りへ正規化する。見出しは`pyfltr.parsing.pytest_nested.pytest_parent_summary_start`で
       親自身のものを特定し、対象の見出し以降のみを走査する
    2. `= FAILURES =`セクションをテスト名区切り（`_ 名前 _`）でブロック分割し、
       既定のトレースバック形式（`--tb=auto`・`--tb=long`）の位置行
       （`<file>:<line>: <例外名>`、フレーム行より後にあるもの）を優先し、
       無ければフレーム行（`file:line: in func`）をプロジェクト内フレーム優先で診断化する
    3. いずれも持たないブロック（xdistワーカークラッシュ等）は
       `worker '<id>' crashed while running '<file>::<test>'`行から`line=0`の診断を生成する
    4. ブロック分割できない場合（`--tb=line`形式）は`<file>:<line>: <message>`行を
       実際の行番号付きで診断化し、summary辞書に同一`(file, message)`があればテスト名を先頭へ併記する
    5. 上記いずれでも拾えなかったsummary辞書の残余エントリを`parse_pytest_from_summary`で
       `line=0`の診断として補完する
    6. 試験の準備・後始末・収集のERROR（一覧の`ERROR`行と`= ERRORS =`節）を
       `parse_pytest_errors`で診断化し、本体の失敗の診断とは別に加える

    経路1の前に、捕捉出力へ混入した子プロセスのpytest実行を
    `pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs`で空行へ置き換える。pytestを子プロセスとして起動し
    出力を取り込むテストでは、親の失敗欄・集計行の内側へ子の失敗欄・集計行が現れるため、
    除外しないと子プロセス側の失敗を親プロセスの実在する失敗として診断化する。
    対象の除外が成立しない構成に備え、経路2〜3では親の失敗一覧に載らないテスト名のブロックを
    除外する安全網を併用する（失敗一覧の見出しを持たない構成・失敗一覧が空の構成では働かせない）。
    親が失敗一覧を出力しない構成では対象の安全網も働かないため、捕捉出力へ残った子の失敗が
    親の失敗として診断化される。子の失敗一覧との突合で除外する案は、親が子と同じテストファイルの
    同名テストを実行する構成において親の実在する失敗を除外するため採らない。

    経路2〜4でテスト名を確定できたつど`pyfltr.parsing.pytest_common.consume_summary_test`で`consumed`
    （summary辞書のキー`(file, test)`の集合）へ登録し、経路5の二重生成を防ぐ。
    同名テストが複数ファイルに存在する場合は診断ファイルと一致する候補を優先して消費し、
    一致が無い場合（フレーム選択がプロジェクト外へフォールバックした場合等）は同名の
    未消費候補へフォールバックする。`_ test_name _`区切りからテスト名を抽出し、message先頭へ
    `<test_name>: `として併記する。pytestの`assert ... == ...`表示はテスト関数名なしでは
    判別が難しいケースが多く、location（file/line）と組み合わせて実質的にnodeid相当の
    判別性を得るため。
    """
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
    """子実行を除外し、親の一覧とERROR診断を確定した入力。"""

    output: str
    paths: pyfltr.parsing.pytest_common.PytestPaths
    failures_start: int
    summary_start: int
    summary: dict[tuple[str, str], str | None]
    error_results: list[ErrorLocation]


def _prepare_pytest_input(output: str, path_base: pathlib.Path | None, start_cwd: pathlib.Path | None) -> _PytestInput:
    """親子実行の除外と、親の一覧・ERRORの解析を行う。"""
    paths = pyfltr.parsing.pytest_common.pytest_paths(output, path_base, start_cwd)
    output = pyfltr.parsing.pytest_nested.mask_pytest_captured_child_runs(output, paths=paths)

    # 失敗欄の開始位置は先頭側を探す。子プロセスのpytestを起動して出力を取り込むテストでは
    # 捕捉出力の中にも同じ見出しが現れるため、自プロセスのものが先に現れる先頭一致を採用する。
    # 失敗一覧の見出しは`pyfltr.parsing.pytest_nested.pytest_parent_summary_start`で親自身のものを特定する。
    failures_start = output.find("= FAILURES =")
    summary_start = pyfltr.parsing.pytest_nested.pytest_parent_summary_start(output)

    # 親自身の失敗一覧を持たず子プロセスのものだけが混入している場合、出力中の`FAILED`行は
    # すべて子のものであり親の失敗一覧は存在しない。対象の行を親の失敗一覧として扱うと
    # 実在しない失敗を診断化するため、親の失敗一覧は空とする。
    # 見出しが1つも無い構成では出力全体を走査する（見出しを欠く構成への耐性を維持するため）。
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
    """ブロックを持たないtb=line形式の位置と一覧を対応付ける。"""
    # ブロック区切りが無い場合（`--tb=line`形式）: `<file>:<line>: <message>`行を直接拾う。
    # 突合は2段構成とする。1段目はファイル・メッセージの両方が一致する候補、
    # 見つからない場合の2段目はメッセージのみ一致する候補へフォールバックする
    # （位置行がsite-packages等の外部パスになる失敗はファイルが一致しないため）。
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
            # doctestの拡張名は[doctest]ブロックの所有者を確定できる経路で扱う。
            if doctest_location:
                continue
            # 標準収集されたテスト自身のtb=line終端はco_filenameの絶対位置になる。
            # FAILEDと対応する終端だけを消費し、本文内の相対位置を一覧へ割り当てない。
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
            ErrorLocation(
                file=file_path,
                line=int(match.group("line")),
                col=None,
                command="pytest",
                message=test_name if doctest_location and test_name else f"{test_name}: {message}" if test_name else message,
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
    """失敗ブロックの位置と理由を解析し、位置を持たない場合はクラッシュ行を扱う。"""
    # フレーム行: file:line: in func_name
    frame_re = re.compile(
        rf"^(?P<file>\S.*?):(?P<line>\d+): (?P<message>in {pyfltr.parsing.pytest_common.PYTEST_FUNCTION_NAME})$", re.MULTILINE
    )
    # エラー行: E   message
    error_re = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)

    # 親の失敗一覧に載らないテスト名の失敗欄は親の失敗ではない。除外の主経路（実行マーカーに
    # よる対応付け）が成立しない構成で捕捉出力へ残った子プロセスの失敗欄を除外する安全網とする。
    # 失敗一覧の見出しを持たない構成では`pyfltr.parsing.pytest_common.parse_pytest_summary`が出力全体を走査し、
    # 捕捉出力に混入した子の`FAILED`行まで拾うため、対象の構成では安全網を働かせない。
    # 集計行が失敗を列挙しない構成（`-r`の指定から`f`を外した場合など）でも
    # 親の失敗欄をすべて除外しないよう、失敗一覧が空の場合は働かせない。
    known_tests = {test for _, test in summary} if summary and summary_start >= 0 else None
    first_captured = pyfltr.parsing.pytest_common.PYTEST_CAPTURED_SECTION_RE.search(failures_section)

    results = []
    for i, match in enumerate(block_matches):
        start = match.end()
        block_end = block_matches[i + 1].start() if i + 1 < len(block_matches) else len(failures_section)
        block = failures_section[start:block_end]
        # ERRORS節のブロック（setup・teardown・収集のERROR）は`parse_pytest_errors`が扱う。
        # 子pytestの失敗欄の見出しが親のERRORブロックより前に現れると失敗欄の範囲へ入るため、失敗として扱わない。
        if pyfltr.parsing.pytest_common.PYTEST_ERROR_HEAD_RE.fullmatch(match.group("test_name").strip()) is not None:
            continue
        # doctestのブロック見出しは`[doctest] <モジュール>.<関数>`形式で、集計行の
        # テスト名（`<モジュール>.<関数>`）と一致しない。接頭辞を除いてから突合しないと
        # summary残余補完で同一の失敗の診断が二重生成される。
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
        # 標準traceback以降は異なる位置の文法になる。連鎖したExceptionGroupでは、
        # その前にあるpytest形式の位置を保つ。先頭からnative形式なら一覧から補完する。
        native = re.search(r"^Traceback \(most recent call last\):$", traceback, re.MULTILINE)
        if native is not None:
            traceback = traceback[: native.start()]
        frames = list(pyfltr.parsing.pytest_common.pytest_positions(frame_re, traceback, block_paths))
        location = pyfltr.parsing.pytest_common.select_pytest_location_line(
            traceback, allow_fallback=not frames, paths=block_paths
        )
        if location is not None and (not frames or location.start() > frames[-1].start()):
            # 既定のトレースバック形式では最終エントリーの位置行がフレーム行より後に現れ、
            # 実際に例外が発生した位置を指す。フレーム行を持たない失敗ではこの行が唯一の
            # 情報源となり、持つ場合も中間エントリーのフレーム行より正確である。
            if pyfltr.parsing.pytest_nested.is_unlisted_child_block(
                known_tests, after_captured=after_captured, test_name=test_name
            ):
                continue
            file_path = pyfltr.paths.normalize_separators(location.group("file"))
            raw_message = pyfltr.parsing.pytest_common.pytest_block_message(block, error_re, location, block_paths)
            results.append(
                ErrorLocation(
                    file=file_path,
                    line=int(location.group("line")),
                    col=None,
                    command="pytest",
                    message=pyfltr.parsing.pytest_common.pytest_join_message(test_name, raw_message),
                )
            )
            pyfltr.parsing.pytest_common.consume_summary_test(summary, consumed, test_name, file_path)
            continue

        if frames:
            if pyfltr.parsing.pytest_nested.is_unlisted_child_block(
                known_tests, after_captured=after_captured, test_name=test_name
            ):
                continue
            # フレーム群から最後のプロジェクト内フレームを選択
            chosen = frames[-1]  # フォールバック: 最後のフレーム
            for frame in reversed(frames):
                if paths.project(frame.group("file")):
                    chosen = frame
                    break
            # 例外の連鎖では複数のエントリーが並ぶ。選んだフレームより後にある最初のエラー行が
            # 対象のエントリーの例外であり、集計行のメッセージとも一致する。ブロック先頭から
            # 探すと内側の例外を報告する。
            error_match = error_re.search(block, chosen.end()) or error_re.search(block)
            raw_message = error_match.group("message").strip() if error_match else ""
            message = pyfltr.parsing.pytest_common.pytest_join_message(test_name, raw_message)
            file_path = pyfltr.paths.normalize_separators(chosen.group("file"))
            results.append(
                ErrorLocation(file=file_path, line=int(chosen.group("line")), col=None, command="pytest", message=message)
            )
            pyfltr.parsing.pytest_common.consume_summary_test(summary, consumed, test_name, file_path)
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
    """xdistのワーカークラッシュ行を、親の一覧と対応付けて診断化する。"""
    # フレーム行が無いブロック: xdistワーカークラッシュを想定して専用行を探す。
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
    # クラッシュ行のテスト名はnodeid表記（`TestX::test_y`）のため、summaryキーと同じ
    # `.`区切りへ正規化してから突合する（正規化しないとクラスベーステストで突合が失敗し
    # summary残余補完で同一テストの診断が二重生成される）。
    pyfltr.parsing.pytest_common.consume_summary_test(summary, consumed, crashed_test.replace("::", "."), file_path)

    return result


def summarize_pytest(output: str) -> str | None:
    """Pytest出力末尾のサマリー行を = パディング除去して抽出する。"""
    match = re.search(r"=+ (.+?) =+\s*$", output)
    if match is None:
        return None
    return match.group(1)
