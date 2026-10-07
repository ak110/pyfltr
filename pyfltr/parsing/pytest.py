"""pytestの親子出力と診断の解析。"""

import re

import pyfltr.paths
import pyfltr.rule_urls
from pyfltr.diagnostics import FILE_PATTERN as _FILE
from pyfltr.diagnostics import ErrorLocation
from pyfltr.parsing.common import is_project_path

_PYTEST_SUMMARY_RE = re.compile(
    rf"^FAILED\s+(?P<file>{_FILE})::(?P<test>[^\s\[]+(?:\[.*?\])?)(?:\s+-\s+(?P<message>.+))?$",
    re.MULTILINE,
)


_PYTEST_CRASH_RE = re.compile(rf"worker '(?P<worker>[^']+)' crashed while running '(?P<file>{_FILE})::(?P<test>[^']+)'")


_PYTEST_TB_LINE_RE = re.compile(
    rf"^(?P<file>{_FILE}):(?P<line>\d+):\s*(?P<message>.+)$",
    re.MULTILINE,
)


_PYTEST_BLOCK_HEAD_RE = re.compile(r"^_+ (?P<test_name>.*?[^\s_].*?) _+$", re.MULTILINE)


_PYTEST_CAPTURED_SECTION_RE = re.compile(r"^-+ Captured .+ -+$", re.MULTILINE)


_PYTEST_LOCATION_LINE_RE = re.compile(rf"^(?P<file>{_FILE}):(?P<line>\d+):(?P<message>.*)$", re.MULTILINE)


_PYTEST_SESSION_START_RE = re.compile(r"^=+ test session starts =+$")


_PYTEST_SESSION_END_RE = re.compile(r"^=+ .* in \d+(?:\.\d+)?s.*=+$")


_PYTEST_QUIET_SESSION_END_RE = re.compile(
    r"^(?:\d+ \w+(?:, \d+ \w+)*|no tests ran) in \d+(?:\.\d+)?s(?: \((?:\d+ days?, )?\d+:\d{2}:\d{2}\))?$"
)


_PYTEST_SUMMARY_HEAD_RE = re.compile(r"^=+ short test summary info =+$")


_PYTEST_NESTED_RUN_MARKER_RES = (
    _PYTEST_BLOCK_HEAD_RE,
    _PYTEST_CAPTURED_SECTION_RE,
    _PYTEST_SESSION_START_RE,
    _PYTEST_SESSION_END_RE,
    _PYTEST_QUIET_SESSION_END_RE,
    _PYTEST_SUMMARY_HEAD_RE,
)


_PYTEST_CONFIG_CONFLICT_RE = re.compile(
    r"^configfile: (?P<used>.+?) \(WARNING: ignoring pytest config in (?P<ignored>.+?)!\)\s*$"
)


_ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


_PYTEST_COLLECTION_RE = re.compile(r"^(?:collecting |collected \d+ items?)")


_PYTEST_SECTION_RE = re.compile(r"^=+ .+ =+$")


def pytest_parent_summary_start(output: str) -> int:
    """親プロセス自身の失敗一覧の見出しの開始位置を返す。見出しを持たない場合は-1を返す。

    pytestを子プロセスとして起動し出力を取り込むテストでは、捕捉出力の内側へ子プロセスの
    失敗一覧の見出しがそのまま現れる。出力中で最後に現れる見出しを無条件に採ると、親が
    失敗一覧を出力しない構成（`-rN`等）で子の見出しを親のものとして採用し、失敗欄の解析範囲が
    そこで打ち切られる。打ち切り以降にある親の実在する失敗は診断から消え、代わりに子の失敗が
    親の失敗として報告される。

    判別は、対象の見出しより後・親の最終集計行より前に入れ子の実行を示す標識
    （`_PYTEST_NESTED_RUN_MARKER_RES`）が現れないことによる。親は失敗一覧を最後の節として
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
        if _PYTEST_SESSION_START_RE.fullmatch(lines[following].rstrip("\r")):
            break
        if is_pytest_nested_run_marker(lines[following]):
            limit = len(lines)
            break
    heads = [index for index, line in enumerate(lines[:limit]) if _PYTEST_SUMMARY_HEAD_RE.fullmatch(line.rstrip())]
    for index in reversed(heads):
        if any(is_pytest_nested_run_marker(lines[following]) for following in range(index + 1, limit)):
            continue
        return sum(len(line) + 1 for line in lines[:index])
    return -1


def is_pytest_nested_run_marker(line: str) -> bool:
    """行が入れ子の実行を示す標識かを返す。

    末尾の空白を除去せずに照合する。既定のトレースバック形式が例外の連鎖のエントリー間へ
    出力する区切り行（`_ _ _ ... _ `）は末尾に空白を伴い、ブロック見出しの照合
    （`_PYTEST_BLOCK_HEAD_RE`の行末の`$`）では一致しない。空白を除去すると対象の区切り行が
    ブロック見出しとして一致し、`--full-trace`付きの中断のように失敗一覧より後へ完全な
    トレースバックが続く出力で、親自身の失敗一覧を子のものと誤判定する。
    """
    return any(pattern.fullmatch(line.rstrip("\r")) for pattern in _PYTEST_NESTED_RUN_MARKER_RES)


def has_pytest_summary_head(output: str) -> bool:
    """出力が失敗一覧の見出しを含むかを返す。親のものか子のものかは区別しない。"""
    return any(_PYTEST_SUMMARY_HEAD_RE.fullmatch(line.rstrip()) for line in output.split("\n"))


def parse_pytest_summary(output: str) -> dict[tuple[str, str], str | None]:
    r"""`short test summary info`の`FAILED <file>::<test> - <message>`行を解析する。

    キーは`(file, test)`。`test`部分は`(?P<test>[^\s\[]+(?:\[.*?\])?)`で、パラメータ化テストの
    角括弧内には空白・ハイフン・角括弧の入れ子を許容する（`test_a[param with space]`・
    `test_param[b - c]`・`test_listid[['a', 'b']]`・`test_nested[list[int] and str]`のように、
    `ids`へリストや型注釈風の文字列を渡すと実際に生成されるIDである）。角括弧の外側は
    空白と`[`を含まないnodeidの制約を`[^\s\[]+`で表し、角括弧内は`\[.*?\]`の非貪欲マッチと
    後続文脈（`\s+-\s+`または行末）へのバックトラックで閉じ位置を確定する。
    `[^\]]*`のように閉じ括弧を越えられない表現にすると、閉じ括弧や入れ子を含むIDの行が
    一切マッチせず、失敗一覧のみが情報源となる`--tb=no`等で対象の失敗が診断から消える。
    `test`は`::`区切り（`TestX::test_y`形式、pytestのnodeid表記）を
    `.`区切り（`TestX.test_y`形式、`= FAILURES =`セクションのブロック見出しの表記）へ
    `.replace("::", ".")`で正規化する。両表記が一致しないとテスト名突合（`consumed`集合）が
    成立せず、summary残余補完で同一テストの診断が二重生成されるため。値は`message`
    （省略時はNone、`--tb=line`経路の値と突合できるよう前後の空白・改行文字を`.strip()`で
    除去して統一する）。走査範囲の決定は呼び出し側が担う（`parse_pytest`が
    `pytest_parent_summary_start`で親自身の失敗一覧の見出しを特定し、対象の見出し以降のみを
    渡す）。テストの捕捉出力に子プロセスpytestの`FAILED ...`行が混入していても、
    実在しない失敗として誤検出しないため。
    """
    summary: dict[tuple[str, str], str | None] = {}
    for match in _PYTEST_SUMMARY_RE.finditer(output):
        file_path = pyfltr.paths.to_cwd_relative(match.group("file"))
        test_name = match.group("test").replace("::", ".")
        raw_message = match.group("message")
        summary[(file_path, test_name)] = raw_message.strip() if raw_message is not None else None
    return summary


def parse_pytest_from_summary(
    summary: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]]
) -> list[ErrorLocation]:
    """summary辞書のうち他経路で拾えなかった残余エントリを`line=0`の診断として補完する。

    `consumed`はsummary辞書のキーそのもの（`(file, test)`の組）の集合である。
    キーの決定は`consume_summary_test`が担い、フレーム選択がプロジェクト外フレーム
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


def consume_summary_test(
    summary: dict[tuple[str, str], str | None],
    consumed: set[tuple[str, str]],
    test_name: str,
    diagnosed_file: str,
) -> None:
    """ブロック解析等で確定したテスト名をsummary辞書と突合し、対応する1件を消費済みにする。

    同名テストが複数ファイルに存在し得るため、診断ファイル（`diagnosed_file`）と一致する候補を
    優先して消費する。一致する候補が無い場合（フレーム選択がプロジェクト外へフォールバックし
    診断ファイルとテスト本来のファイルが一致しない等）は、同名の未消費候補を1件先頭から選んで
    消費する。マッチが1件も無い場合は何もしない（summaryに対応エントリが無い、または
    `= FAILURES =`セクションのみでsummaryが空の場合）。
    """
    fallback_key: tuple[str, str] | None = None
    for key in summary:
        sum_file, sum_test = key
        if sum_test != test_name or key in consumed:
            continue
        if sum_file == diagnosed_file:
            consumed.add(key)
            return
        if fallback_key is None:
            fallback_key = key
    if fallback_key is not None:
        consumed.add(fallback_key)


def match_truncated_summary(
    summary: dict[tuple[str, str], str | None], consumed: set[tuple[str, str]], message: str
) -> tuple[str, str] | None:
    """切り詰められた集計行のメッセージを位置行のメッセージへ前方一致で突合する。

    pytestの集計行は失敗理由が端末幅に収まらないとき末尾を`...`へ置き換えて出力する。
    完全一致だけで突合すると、この失敗が`consumed`へ登録されず、位置行由来の診断と
    残余補完による`line=0`の診断が同一の失敗に対して二重生成される。

    前方一致は別々の失敗が同じ接頭辞を持つ場合に取り違えるため、切り詰めを示す`...`で
    終わる候補に限定し、未消費の候補が複数一致する場合は突合しない。
    """
    matched = [
        key
        for key, sum_message in summary.items()
        if key not in consumed
        and sum_message is not None
        and sum_message.endswith("...")
        and message.startswith(sum_message[: -len("...")])
    ]
    return matched[0] if len(matched) == 1 else None


def mask_pytest_captured_child_runs(output: str) -> str:
    """テストの捕捉出力へ混入した子プロセスのpytest実行を空行へ置き換える。

    pytestを子プロセスとして起動し出力を取り込むテストでは、親の失敗欄・集計行の内側へ
    子プロセスの失敗欄・集計行がそのまま現れる。除外しないと子プロセス側の失敗が
    親プロセスの実在する失敗として診断化され、存在しないファイル・行番号が報告される。

    除外する領域は、捕捉出力の節見出し（`-+ Captured .+ -+`）より後に現れた
    子プロセスの実行開始行（`=+ test session starts =+`）から、対応する終了集計行
    （`=+ ... in <秒数>s ... =+`）までとする。開始・終了の双方をpytest自身が出力する
    マーカーで判定するため、除外範囲は子プロセスの実行1回分に正確に一致する。

    子プロセスのブロック見出し・位置行・捕捉出力の節見出しは親のものと書式が同一で、
    構文だけでは判別できない。
    親の集計行との突合で終端を判定すると、親子で同名のテストが失敗する場合や、
    ブロック見出しを持たない`--tb=line`形式で終端を決められず、本来の失敗まで除外する。
    実行マーカーによる判定はこれらの構成に依存しない。

    終了集計行を`pytest_parent_tail_index`が返す上限より前に見つけられない場合、対象の領域は
    除外しない。子プロセスが異常終了・打ち切りで終了集計行を欠いたまま終わると、親自身の
    最終集計行を終端として誤採用し、その間にある親の失敗と失敗一覧をすべて失うため、
    上限を越える終端候補は採用せず旧来の挙動へ縮退させる。
    捕捉出力が子プロセスのpytest実行を含まない場合も同じ理由で除外しない。

    次の3つの構成では開始・終了のマーカーが成立せず除外できない。いずれも子プロセス側の
    失敗が親の失敗として混入するが、本処理の導入前と同じ結果であり退行にはあたらない。
    親が失敗一覧を出力し、かつ子のテスト名が親の失敗一覧に無い場合は`parse_pytest`の
    安全網（失敗一覧に載らないテスト名の失敗欄を除外する）が働き、架空の診断が残らない。
    親子で同名のテストが失敗する構成では安全網も働かない。

    - 子プロセスを`-q`で起動した場合。実行開始行を出力しないため開始位置を確定できない
    - 親プロセスが`-s`で動く場合。子の出力が捕捉されず節見出しが出ないうえ、
      子の実行開始行が親の進捗行と同一行へ連結される
    - 子プロセス側の失敗したテストが出力を持つ場合。子自身の捕捉出力の節見出しが
      親の節見出しと同一書式で現れ、終端未確定の開始位置を破棄する規則が発火する。
      破棄規則を子の失敗欄より後で止めると、未終端の実行が親のブロック境界を越えて
      後続の節の終了集計行と対になり、間にある親の失敗を除外する側の誤りへ倒れる

    行を削除せず空行へ置き換えるのは、以降の正規表現探索が扱う行構造を保つためである。
    """
    # 行の分割は`\n`のみを区切りとする。`str.splitlines`はフォームフィード等でも分割するため、
    # 以降の正規表現探索（`re.MULTILINE`は`\n`のみを行区切りとする）と行の対応が崩れる。
    lines = output.split("\n")
    limit = pytest_parent_tail_index(lines)
    masked = [False] * len(lines)
    after_captured = False
    start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.rstrip()
        if _PYTEST_CAPTURED_SECTION_RE.fullmatch(stripped):
            after_captured = True
            # 1回の実行の出力が2つの捕捉出力の節へまたがることはないため、節の切れ目で
            # 終端未確定の開始位置を破棄する。破棄しないと、別の節に現れた終了集計行
            # （子の出力の末尾だけを表示した場合など）と対になり、間の親の失敗を除外する。
            start = None
            continue
        if after_captured and _PYTEST_SESSION_START_RE.fullmatch(stripped):
            # 終端未確定のまま次の実行開始行に達した場合は、そちらへ開始位置を移す。
            # 終了集計行を欠いた実行の開始位置を保持したままにすると、後続の別の実行の
            # 終了集計行と対になり、その間にある親の失敗まで除外してしまう。
            start = i
            continue
        if start is None:
            continue
        if i >= limit:
            # 上限へ到達した領域は終端を確定できなかったものとして扱い、除外しない。
            start = None
            continue
        if _PYTEST_SESSION_END_RE.fullmatch(stripped):
            for j in range(start, i + 1):
                masked[j] = True
            start = None
    return "\n".join("" if is_masked else line for is_masked, line in zip(masked, lines, strict=True))


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
        if _PYTEST_SESSION_END_RE.fullmatch(stripped) or _PYTEST_QUIET_SESSION_END_RE.fullmatch(stripped):
            return i
    return len(lines)


def select_pytest_location_line(block: str, *, allow_fallback: bool) -> re.Match[str] | None:
    """失敗ブロックから既定のトレースバック形式の位置行を選ぶ。

    既定のトレースバック形式（`--tb=auto`・`--tb=long`）は各エントリーの末尾へ位置行を
    出力する。例外を送出したエントリーは例外名を伴い、呼び出し側のエントリーは伴わない。
    フレームが1つだけの失敗（`assert`直書きの典型的な失敗）ではフレーム行
    （`<file>:<line>: in <func>`）が現れないため、位置行が唯一の行番号の情報源となる。

    走査範囲は捕捉出力の節見出しより前に限る。節より後は任意のテキストであり、
    位置行と同じ書式の行が現れても失敗の位置ではない。

    例外の連鎖では例外名を伴う位置行が複数現れる。最後のものが実際に失敗を起こした例外の
    位置であり、集計行のメッセージとも一致する。プロジェクト内のものを優先して末尾側から
    選ぶのはフレーム解析と同じ方針による。

    `allow_fallback`はブロックがフレーム行を持たない場合に`True`とする。この場合は他に
    行番号の情報源が無いため、プロジェクト内の位置行が例外名を伴わないものしか無ければ
    それを採用し（`--tb=short`が選ぶプロジェクト内フレームと同じ位置を指す）、
    プロジェクト内の位置行が皆無ならプロジェクト外の位置行まで採る。
    フレーム行を持つ場合は`False`とし、プロジェクト内フレームを優先する選択を崩さない。
    """
    section = _PYTEST_CAPTURED_SECTION_RE.search(block)
    scan_target = block[: section.start()] if section is not None else block
    typed: list[re.Match[str]] = []
    bare: list[re.Match[str]] = []
    for match in _PYTEST_LOCATION_LINE_RE.finditer(scan_target):
        (typed if match.group("message").strip() else bare).append(match)
    for candidates in (typed, bare) if allow_fallback else (typed,):
        for match in reversed(candidates):
            if is_project_path(pyfltr.paths.to_cwd_relative(match.group("file"))):
                return match
    return typed[-1] if typed and allow_fallback else None


def pytest_block_message(block: str, error_re: re.Pattern[str], location: re.Match[str]) -> str:
    """位置行に対応するエラー行（`E   <message>`）の本文を返す。

    例外の連鎖では複数のエントリーがブロック内に並ぶ。直前の位置行より後にある最初の
    エラー行が対象のエントリーの例外を表すため、そこから採る。直前の位置行が無い場合
    （エントリーが1つだけの場合）はブロック先頭から探す。
    """
    previous_end = 0
    for match in _PYTEST_LOCATION_LINE_RE.finditer(block[: location.start()]):
        previous_end = match.end()
    error_match = error_re.search(block, previous_end) or error_re.search(block)
    return error_match.group("message").strip() if error_match is not None else ""


def pytest_join_message(test_name: str, raw_message: str) -> str:
    """テスト名と失敗理由の本文を診断のメッセージへ組み立てる。

    pytestの`assert ... == ...`表示はテスト関数名なしでは判別が難しいため、本文の先頭へ
    テスト名を併記する。doctestのように本文（`E   <message>`行）を持たない失敗では
    テスト名のみとする（`<名前>: `で終わる中身の無いメッセージにしない）。
    """
    if not test_name:
        return raw_message
    return f"{test_name}: {raw_message}" if raw_message else test_name


def is_unlisted_child_block(known_tests: set[str] | None, *, after_captured: bool, test_name: str) -> bool:
    """失敗欄のブロックが親の失敗ではない（捕捉出力へ混入した子プロセスのもの）かを判定する。

    子プロセスの失敗欄は必ず親の捕捉出力の節見出しより後に現れ、親の失敗一覧には載らない。
    両方を満たすブロックのみ親の失敗ではないと判定する。節見出しより前のブロックを
    対象に含めると、親自身が最終集計行を持たず子の失敗一覧だけが出力に残る構成で、
    親の失敗欄をすべて除外する。

    `known_tests`がNoneの構成（親の失敗一覧を特定できない場合）では判定しない。
    """
    return known_tests is not None and after_captured and test_name not in known_tests


def parse_pytest(output: str) -> list[ErrorLocation]:
    """Pytest出力をパース。

    次の優先順で情報源を扱い、いずれの経路でも失敗理由の本文を可能な限り保持する。

    1. `short test summary info`の`FAILED <file>::<test> - <message>`行を
       `(file, test) -> message`辞書として先に収集する（メッセージ補完・突合用）。
       `test`は`.`区切りへ正規化する。見出しは`pytest_parent_summary_start`で
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

    経路1の前に、捕捉出力へ混入した子プロセスのpytest実行を
    `mask_pytest_captured_child_runs`で空行へ置き換える。pytestを子プロセスとして起動し
    出力を取り込むテストでは、親の失敗欄・集計行の内側へ子の失敗欄・集計行が現れるため、
    除外しないと子プロセス側の失敗を親プロセスの実在する失敗として診断化する。
    対象の除外が成立しない構成に備え、経路2〜3では親の失敗一覧に載らないテスト名のブロックを
    除外する安全網を併用する（失敗一覧の見出しを持たない構成・失敗一覧が空の構成では働かせない）。
    親が失敗一覧を出力しない構成では対象の安全網も働かないため、捕捉出力へ残った子の失敗が
    親の失敗として診断化される。子の失敗一覧との突合で除外する案は、親が子と同じテストファイルの
    同名テストを実行する構成において親の実在する失敗を除外するため採らない。

    経路2〜4でテスト名を確定できたつど`consume_summary_test`で`consumed`
    （summary辞書のキー`(file, test)`の集合）へ登録し、経路5の二重生成を防ぐ。
    同名テストが複数ファイルに存在する場合は診断ファイルと一致する候補を優先して消費し、
    一致が無い場合（フレーム選択がプロジェクト外へフォールバックした場合等）は同名の
    未消費候補へフォールバックする。`_ test_name _`区切りからテスト名を抽出し、message先頭へ
    `<test_name>: `として併記する。pytestの`assert ... == ...`表示はテスト関数名なしでは
    判別が難しいケースが多く、location（file/line）と組み合わせて実質的にnodeid相当の
    判別性を得るため。
    """
    output = mask_pytest_captured_child_runs(output)

    # 失敗欄の開始位置は先頭側を探す。子プロセスのpytestを起動して出力を取り込むテストでは
    # 捕捉出力の中にも同じ見出しが現れるため、自プロセスのものが先に現れる先頭一致を採用する。
    # 失敗一覧の見出しは`pytest_parent_summary_start`で親自身のものを特定する。
    failures_start = output.find("= FAILURES =")
    summary_start = pytest_parent_summary_start(output)

    # 親自身の失敗一覧を持たず子プロセスのものだけが混入している場合、出力中の`FAILED`行は
    # すべて子のものであり親の失敗一覧は存在しない。対象の行を親の失敗一覧として扱うと
    # 実在しない失敗を診断化するため、親の失敗一覧は空とする。
    # 見出しが1つも無い構成では出力全体を走査する（見出しを欠く構成への耐性を維持するため）。
    if summary_start >= 0:
        summary = parse_pytest_summary(output[summary_start:])
    elif has_pytest_summary_head(output):
        summary = {}
    else:
        summary = parse_pytest_summary(output)
    consumed: set[tuple[str, str]] = set()

    if failures_start < 0:
        return parse_pytest_from_summary(summary, consumed)

    end = summary_start if summary_start > failures_start else len(output)
    failures_section = output[failures_start:end]

    block_matches = list(_PYTEST_BLOCK_HEAD_RE.finditer(failures_section))

    if not block_matches:
        # ブロック区切りが無い場合（`--tb=line`形式）: `<file>:<line>: <message>`行を直接拾う。
        # 突合は2段構成とする。1段目はファイル・メッセージの両方が一致する候補、
        # 見つからない場合の2段目はメッセージのみ一致する候補へフォールバックする
        # （位置行がsite-packages等の外部パスになる失敗はファイルが一致しないため）。
        results: list[ErrorLocation] = []
        for match in _PYTEST_TB_LINE_RE.finditer(failures_section):
            file_path = pyfltr.paths.to_cwd_relative(match.group("file"))
            message = match.group("message").strip()
            matched_key: tuple[str, str] | None = None
            for key, sum_message in summary.items():
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
                matched_key = match_truncated_summary(summary, consumed, message)
            test_name = matched_key[1] if matched_key is not None else None
            if matched_key is not None:
                consumed.add(matched_key)
            results.append(
                ErrorLocation(
                    file=file_path,
                    line=int(match.group("line")),
                    col=None,
                    command="pytest",
                    message=f"{test_name}: {message}" if test_name else message,
                )
            )
        results.extend(parse_pytest_from_summary(summary, consumed))
        return results

    # フレーム行: file:line: in func_name
    frame_re = re.compile(rf"^(?P<file>{_FILE}):(?P<line>\d+): in .+$", re.MULTILINE)
    # エラー行: E   message
    error_re = re.compile(r"^E\s+(?P<message>.+)$", re.MULTILINE)

    # 親の失敗一覧に載らないテスト名の失敗欄は親の失敗ではない。除外の主経路（実行マーカーに
    # よる対応付け）が成立しない構成で捕捉出力へ残った子プロセスの失敗欄を除外する安全網とする。
    # 失敗一覧の見出しを持たない構成では`parse_pytest_summary`が出力全体を走査し、
    # 捕捉出力に混入した子の`FAILED`行まで拾うため、対象の構成では安全網を働かせない。
    # 集計行が失敗を列挙しない構成（`-r`の指定から`f`を外した場合など）でも
    # 親の失敗欄をすべて除外しないよう、失敗一覧が空の場合は働かせない。
    known_tests = {test for _, test in summary} if summary and summary_start >= 0 else None
    first_captured = _PYTEST_CAPTURED_SECTION_RE.search(failures_section)

    results = []
    for i, match in enumerate(block_matches):
        start = match.end()
        block_end = block_matches[i + 1].start() if i + 1 < len(block_matches) else len(failures_section)
        block = failures_section[start:block_end]
        # doctestのブロック見出しは`[doctest] <モジュール>.<関数>`形式で、集計行の
        # テスト名（`<モジュール>.<関数>`）と一致しない。接頭辞を除いてから突合しないと
        # summary残余補完で同一の失敗の診断が二重生成される。
        test_name = match.group("test_name").strip().removeprefix("[doctest] ")
        after_captured = first_captured is not None and match.start() > first_captured.start()

        frames = list(frame_re.finditer(block))
        location = select_pytest_location_line(block, allow_fallback=not frames)
        if location is not None and (not frames or location.start() > frames[-1].start()):
            # 既定のトレースバック形式では最終エントリーの位置行がフレーム行より後に現れ、
            # 実際に例外が発生した位置を指す。フレーム行を持たない失敗ではこの行が唯一の
            # 情報源となり、持つ場合も中間エントリーのフレーム行より正確である。
            if is_unlisted_child_block(known_tests, after_captured=after_captured, test_name=test_name):
                continue
            file_path = pyfltr.paths.to_cwd_relative(location.group("file"))
            raw_message = pytest_block_message(block, error_re, location)
            results.append(
                ErrorLocation(
                    file=file_path,
                    line=int(location.group("line")),
                    col=None,
                    command="pytest",
                    message=pytest_join_message(test_name, raw_message),
                )
            )
            consume_summary_test(summary, consumed, test_name, file_path)
            continue

        if frames:
            if is_unlisted_child_block(known_tests, after_captured=after_captured, test_name=test_name):
                continue
            # フレーム群から最後のプロジェクト内フレームを選択
            chosen = frames[-1]  # フォールバック: 最後のフレーム
            for frame in reversed(frames):
                if is_project_path(pyfltr.paths.to_cwd_relative(frame.group("file"))):
                    chosen = frame
                    break
            # 例外の連鎖では複数のエントリーが並ぶ。選んだフレームより後にある最初のエラー行が
            # 対象のエントリーの例外であり、集計行のメッセージとも一致する。ブロック先頭から
            # 探すと内側の例外を報告する。
            error_match = error_re.search(block, chosen.end()) or error_re.search(block)
            raw_message = error_match.group("message").strip() if error_match else ""
            message = pytest_join_message(test_name, raw_message)
            file_path = pyfltr.paths.to_cwd_relative(chosen.group("file"))
            results.append(
                ErrorLocation(file=file_path, line=int(chosen.group("line")), col=None, command="pytest", message=message)
            )
            consume_summary_test(summary, consumed, test_name, file_path)
            continue

        # フレーム行が無いブロック: xdistワーカークラッシュを想定して専用行を探す。
        crash_match = _PYTEST_CRASH_RE.search(block)
        if crash_match is None:
            continue
        file_path = pyfltr.paths.to_cwd_relative(crash_match.group("file"))
        crashed_test = crash_match.group("test")
        if is_unlisted_child_block(known_tests, after_captured=after_captured, test_name=crashed_test.replace("::", ".")):
            continue
        results.append(
            ErrorLocation(
                file=file_path,
                line=0,
                col=None,
                command="pytest",
                message=(
                    f"{crashed_test}: worker '{crash_match.group('worker')}' crashed while running {file_path}::{crashed_test}"
                ),
            )
        )
        # クラッシュ行のテスト名はnodeid表記（`TestX::test_y`）のため、summaryキーと同じ
        # `.`区切りへ正規化してから突合する（正規化しないとクラスベーステストで突合が失敗し
        # summary残余補完で同一テストの診断が二重生成される）。
        consume_summary_test(summary, consumed, crashed_test.replace("::", "."), file_path)

    results.extend(parse_pytest_from_summary(summary, consumed))
    return results


def summarize_pytest(output: str) -> str | None:
    """Pytest出力末尾のサマリー行を = パディング除去して抽出する。"""
    match = re.search(r"=+ (.+?) =+\s*$", output)
    if match is None:
        return None
    return match.group(1)


def is_pytest_header_end(line: str) -> bool:
    """与えられた行がpytestのヘッダー領域の終端に当たるかを判定する。

    ヘッダー領域は実行開始行から始まり、収集開始行・節見出し・入れ子実行の標識のいずれかで終わる。
    収集開始行だけを終端とすると、対象の行を出力しない構成でヘッダー領域が閉じない。
    pytest-xdistは収集開始行の代わりに`created: N/M workers`と`N workers [M items]`を出力するため、
    実際に生じる構成である。閉じないまま走査を続けると、捕捉出力へ混入した子プロセスの
    `rootdir:`と`configfile:`の対を親のヘッダーとして読み、子側の競合を親の競合として報告する。
    """
    return (
        _PYTEST_COLLECTION_RE.match(line) is not None
        or _PYTEST_SECTION_RE.fullmatch(line) is not None
        or is_pytest_nested_run_marker(line)
    )


def detect_pytest_config_conflict(output: str) -> str | None:
    """pytestの出力から設定ファイル競合を検出し、警告メッセージを返す。

    競合がない場合、およびヘッダー行を含まない出力（`-q`・`--no-header`指定時）は`None`を返す。
    副作用を持たせず、警告の発行は呼び出し側が担う。

    競合の通知はpytest 9.0以降が出力する。それより前の版は後続候補の設定ファイルを
    無言で無視するため、本関数は何も検出しない。

    探索対象は親プロセス自身のヘッダー領域に限る。pytestを子プロセスとして起動するテストでは
    子のヘッダーがそのまま捕捉出力へ現れるため、出力中のどこにある実行開始行も
    ヘッダーの始まりとして扱うと、親が`-q`・`--no-header`で動く場合に
    子側の競合を親の競合として報告してしまう。
    実行開始行より前にヘッダー領域の終端に当たる行が現れた場合は、
    既にテスト実行の段階へ入っているため親のヘッダーを持たない出力として扱う。
    実行開始行そのものより前に警告等の前置きが出る構成（標準エラー出力の併合など）があるため、
    出力の先頭行であることは要求しない。

    親が`-q`と`-s`を併用し、かつ子プロセスとしてpytestを起動する構成では、
    子の競合を親の競合として報告する。`-q`で親のヘッダーが出ず、`-s`で子の出力が
    捕捉されないため、子のヘッダーが出力の先頭の行群となり、その前に打ち切りの根拠となる
    行が現れない。親子のヘッダーは書式が同一で、テキストだけでは判別できない。
    `--no-header`は単独では該当しない。対象の指定でも実行開始行自体は出力されるため、
    親のヘッダー開始行が常に先に見つかる。
    """
    lines = [_ANSI_SGR_RE.sub("", line).strip() for line in output.splitlines()]
    header_start: int | None = None
    for index, line in enumerate(lines):
        if _PYTEST_SESSION_START_RE.fullmatch(line):
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
        match = _PYTEST_CONFIG_CONFLICT_RE.match(line)
        if match is None:
            previous_line = line
            continue
        if not previous_line.startswith("rootdir: "):
            # pytestは`rootdir:`と`configfile:`を同一のヘッダー行群として隣接出力する。
            # 直前行での限定により、ヘッダー領域へ紛れ込んだ同形の行を競合と誤認しない。
            previous_line = line
            continue
        used = match.group("used")
        ignored = match.group("ignored")
        return f"pytest: 設定ファイルが競合しています。採用: {used} / 無視: {ignored}"
    return None
