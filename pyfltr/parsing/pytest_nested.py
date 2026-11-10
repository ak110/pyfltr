"""pytestの親子実行区間の判定。"""

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
    """親プロセス自身の失敗一覧の見出しの開始位置を返す。見出しを持たない場合は-1を返す。

    pytestを子プロセスとして起動し出力を取り込むテストでは、捕捉出力の内側へ子プロセスの
    失敗一覧の見出しがそのまま現れる。出力中で最後に現れる見出しを無条件に採ると、親が
    失敗一覧を出力しない構成（`-rN`等）で子の見出しを親のものとして採用し、失敗欄の解析範囲が
    そこで打ち切られる。打ち切り以降にある親の実在する失敗は診断から消え、代わりに子の失敗が
    親の失敗として報告される。

    判別は、対象の見出しより後・親の最終集計行より前に入れ子の実行を示す標識
    （`PYTEST_NESTED_RUN_MARKER_RES`）が現れないことによる。親は失敗一覧を最後の節として
    出力するため、標識が後続する見出しは親のものではない。候補は親の最終集計行より前の
    見出しに限り、末尾側から順に判定して最初に条件を満たしたものを採用する。

    上限として採った集計行より後に標識が現れる場合、対象の集計行は親自身の最終集計行ではなく
    捕捉出力へ混入した子のものである。親が最終集計行を持たないまま出力が終わる構成で起こる。
    この場合は上限を出力の末尾へ広げて探し直す。上限を確定できないことを理由に判別を諦めると、
    親自身の失敗一覧を持つ構成でも失敗一覧が空となり、捕捉出力へ残った子の失敗を除外する
    安全網が働かなくなる。

    上限より後の走査は実行の開始行に達した時点で打ち切る。pyfltrは子孫プロセスの出力を
    ストリームの終端まで読むため、親の実行が終わった後に打ち切られた孫プロセスの実行が
    同じ出力へ続くことがある。対象の実行の標識で上限を広げると、失敗一覧のみを情報源とする
    構成（`--tb=no`等）で親の失敗をすべて失う。

    子プロセスが終了集計行も後続の親の失敗欄も持たないまま出力の末尾に達する構成では、
    子の見出しが条件を満たし親のものとして採られる。対象の構成は除外の主経路も安全網も
    成立しない既知の縮退であり、判別条件の追加で新たに生じるものではない。
    """
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
    """行が入れ子の実行を示す標識かを返す。

    末尾の空白を除去せずに照合する。既定のトレースバック形式が例外の連鎖のエントリー間へ
    出力する区切り行（`_ _ _ ... _ `）は末尾に空白を伴い、ブロック見出しの照合
    （`pyfltr.parsing.pytest_common.PYTEST_BLOCK_HEAD_RE`の行末の`$`）では一致しない。空白を除去すると対象の区切り行が
    ブロック見出しとして一致し、`--full-trace`付きの中断のように失敗一覧より後へ完全な
    トレースバックが続く出力で、親自身の失敗一覧を子のものと誤判定する。
    """
    return any(pattern.fullmatch(line.rstrip("\r")) for pattern in PYTEST_NESTED_RUN_MARKER_RES)


def has_pytest_summary_head(output: str) -> bool:
    """出力が失敗一覧の見出しを含むかを返す。親のものか子のものかは区別しない。"""
    return any(PYTEST_SUMMARY_HEAD_RE.fullmatch(line.rstrip()) for line in output.split("\n"))


def pytest_child_run_ranges(
    output: str, *, paths: pyfltr.parsing.pytest_common.PytestPaths | None = None
) -> list[tuple[int, int]]:
    """親の最終集計より前で閉じた子の実行区間（行番号、両端を含む）を返す。

    開始行をスタックへ積み、終了集計行を直近の開始行と対応付ける。
    親の進捗行へ連結された開始行と、ヘッダーを持たない親の開始前に閉じる子も扱う。
    親の失敗一覧と子自身の失敗一覧を照合し、未終端の子が親の失敗ブロックを
    またいで後続の別の実行の終端と対応することを防ぐ。
    設定競合・失敗診断・所要時間の帰属は、この判定を共用する。
    """
    paths = paths or pyfltr.parsing.pytest_common.pytest_paths(output)
    lines = output.split("\n")
    limit = pytest_parent_tail_index(lines)
    # `-qq`は親の終了集計を省く。末尾の集計より後に親の失敗欄が続けば、
    # 対象の集計は子の終端であり、除外範囲の上限を出力末尾へ広げる。
    for following in lines[limit + 1 :]:
        if re.search(r"=+ test session starts =+$", following.rstrip()):
            break
        if is_pytest_nested_run_marker(following):
            limit = len(lines)
            break
    # 親が`=`の埋めを伴う最終集計で終わり、出力の最初の空でない行が開始行そのものである場合、
    # 親は通常表示で動いており、その開始行は親自身のものである。
    # 親自身の開始行を積むと、開始行を持たない子（`-q`）の終了集計が親の開始行と対応し、
    # 親の失敗欄を含む区間が子の実行として除外される。親が失敗一覧を持たない構成（`-rN`）では
    # 親の失敗ブロックの保護も働かないため、親自身の開始行を対応付けの候補から外す。
    # 先頭が開始行でない出力（失敗欄以降の抜粋など）は、最初の開始行が子のものであり得るため対象外とする。
    parent_header_index = -1
    if limit < len(lines) and PYTEST_SESSION_END_RE.fullmatch(
        pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", lines[limit]).rstrip()
    ):
        first = next((index for index, line in enumerate(lines) if line.strip()), -1)
        if first >= 0 and re.fullmatch(
            r"=+ test session starts =+", pyfltr.parsing.pytest_common.ANSI_SGR_RE.sub("", lines[first]).strip()
        ):
            # `-qq`の親は自身の開始行も最終集計も出さず、先頭の開始行と末尾の集計がどちらも子のものになり得る。
            # `=`の埋めを伴う開始行と終了集計だけで対応を取り、先頭の開始行が親の最終集計と対応する場合に限り親とみなす。
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


def summary_tests(output: str) -> set[tuple[str, str]]:
    """失敗一覧の`FAILED`行と、試験名を持つ`ERROR`行の`(file, test)`を返す。親子の実行区間の判別に使う。"""
    return set(pyfltr.parsing.pytest_common.parse_pytest_summary(output)) | {
        key for key in pyfltr.parsing.pytest_common.parse_pytest_error_summary(output) if key[1]
    }


def crosses_pytest_parent_blocks(
    candidate: str, parent_tests: set[tuple[str, str]], parent_files: set[str], paths: pyfltr.parsing.pytest_common.PytestPaths
) -> bool:
    """候補の子実行が、子自身の一覧に無い親の失敗ブロックを含むかを返す。"""
    summary_start = pytest_parent_summary_start(candidate)
    child_tests = summary_tests(candidate[summary_start:] if summary_start >= 0 else candidate)
    blocks = list(pyfltr.parsing.pytest_common.PYTEST_BLOCK_HEAD_RE.finditer(candidate))
    for index, block in enumerate(blocks):
        end = blocks[index + 1].start() if index + 1 < len(blocks) else len(candidate)
        test_name = block["test_name"].strip().removeprefix("[doctest] ")
        error_head = pyfltr.parsing.pytest_common.PYTEST_ERROR_HEAD_RE.fullmatch(test_name)
        if error_head is not None and error_head.group("test") is not None:
            test_name = error_head.group("test").strip()
            # setup・teardownのERRORは共有fixtureなど試験ファイル外の位置を指すため、
            # 位置のファイルでは親の失敗と対応付けられない。
            # 親の一覧にあり子の一覧に無い試験名のERRORブロックを含む候補は、親の実行の一部とみなす。
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
    """共通の実行区間判定で子の実行を空行へ置き換える。

    捕捉出力と親の`-s`系の出力を同じ開始・終了の対応で扱う。
    行構造を保ち、残る位置行の解析へ渡す。

    開始行を持たない子（`-q`）と、ヘッダーの無い親が子の終了直後に打ち切られた出力は
    親子を判別できない。後者は通常の冗長度の親1回分と同じ出力になる。
    """
    lines = output.split("\n")
    for start, end in pytest_child_run_ranges(output, paths=paths):
        lines[start : end + 1] = [""] * (end - start + 1)
    return "\n".join(lines)


def pytest_parent_tail_index(lines: list[str]) -> int:
    """親プロセス自身の最終集計行の位置を返す。子プロセスの実行を除外する範囲の上限として使う。

    最終集計行のうち末尾のものを採用する。親の最終集計行は親の失敗欄・失敗一覧より後に出るため、
    捕捉出力へ混入した子プロセスの集計行は必ずこれより前に位置する。末尾側を採用しないと
    親の失敗欄の途中で上限に達し、子プロセスの除外が成立しなくなる。

    親が`-q`で動く場合の最終集計行は`=`の埋めを伴わないため、対象の形式も併せて探す。
    `=`の埋めを伴う形式だけを探すと、`-q`の親では出力中で最後に一致するのが
    捕捉出力へ混入した子の集計行となり、上限が子の終端そのものを指して除外が成立しなくなる。

    最終集計行を持たない構成では上限を設けない。
    """
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].rstrip()
        if PYTEST_SESSION_END_RE.fullmatch(stripped) or PYTEST_QUIET_SESSION_END_RE.fullmatch(stripped):
            return i
    return len(lines)


def is_unlisted_child_block(known_tests: set[str] | None, *, after_captured: bool, test_name: str) -> bool:
    """失敗欄のブロックが親の失敗ではない（捕捉出力へ混入した子プロセスのもの）かを判定する。

    子プロセスの失敗欄は必ず親の捕捉出力の節見出しより後に現れ、親の失敗一覧には載らない。
    両方を満たすブロックのみ親の失敗ではないと判定する。節見出しより前のブロックを
    対象に含めると、親自身が最終集計行を持たず子の失敗一覧だけが出力に残る構成で、
    親の失敗欄をすべて除外する。

    `known_tests`がNoneの構成（親の失敗一覧を特定できない場合）では判定しない。
    """
    return known_tests is not None and after_captured and test_name not in known_tests
