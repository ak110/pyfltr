"""pytestの一覧と位置を解析する低位の補助。"""

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


# 試験の準備・後始末・収集のERROR。収集エラーは試験名を持たない（`ERROR <file>`）。
PYTEST_ERROR_SUMMARY_RE = re.compile(
    r"^ERROR\s+(?P<file>\S.*?)(?:::(?P<test>[^\s\[]+(?:\[.*?\])?))?(?:\s+-\s+(?P<message>.+))?$",
    re.MULTILINE,
)


# ERRORS節のブロック見出し（`_ ERROR at setup of <test> _`等）から区切り記号を除いた本文。
PYTEST_ERROR_HEAD_RE = re.compile(r"ERROR at (?P<phase>setup|teardown) of (?P<test>.+)|ERROR collecting (?P<file>.+)")


PYTEST_BLOCK_HEAD_RE = re.compile(r"^_+ (?P<test_name>.*?[^\s_].*?) _+$", re.MULTILINE)


PYTEST_CAPTURED_SECTION_RE = re.compile(r"^-+ Captured .+ -+$", re.MULTILINE)


# エントリーの末尾は空欄・例外型名、簡略フレームは`in <関数>`になる。
# 引数の表示（例: `value = '10:20:30'`）も行頭から始まるため、末尾の構造まで照合する。
PYTEST_FUNCTION_NAME = r"(?:[\w.]+|<[\w ]+>)"


PYTEST_EXCEPTION_NAME = r"[^\W\d][\w.]*"


# 基準版で根拠なしに扱っていた名前の集合。削除済みファイルの再解析にも使う。
PYTEST_LEGACY_FILE_RE = re.compile(r"(?:[A-Za-z]:)?[^\s:]+")


PYTEST_LOCATION_LINE_RE = re.compile(
    rf"^(?P<file>\S.*?):(?P<line>\d+):"
    rf"(?P<message>[ \t]*(?:in {PYTEST_FUNCTION_NAME}|{PYTEST_EXCEPTION_NAME})?)[ \t]*$",
    re.MULTILINE,
)


@dataclasses.dataclass
class PytestPaths:
    """同じ実行の一覧・起点・実ファイルに基づく位置候補の根拠。"""

    known_files: set[str]
    path_base: pathlib.Path | None = None
    start_cwd: pathlib.Path | None = None
    reported_root: str | None = None
    allow_expanded: bool = False

    def reported_relative(self, file: str) -> str | None:
        """保存出力を移動した場合も、出力に記録されたrootdirとの対応を保つ。"""
        if self.reported_root is None:
            return None
        try:
            return str(pathlib.PurePosixPath(file).relative_to(self.reported_root))
        except ValueError:
            return None

    def known(self, file: str) -> bool:
        """一覧の相対名と絶対位置の対応を、名前全体で照合する。"""
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
        """明示された解析基準から実ファイルを確認する。"""
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
        """基準版の名前を保持し、拡張名は有効な失敗区間の対象テストへ限定する。"""
        file = pyfltr.paths.normalize_separators(file)
        if file.startswith("<"):
            return False
        return PYTEST_LEGACY_FILE_RE.fullmatch(file) is not None or (self.allow_expanded and self.known(file))

    def project(self, file: str) -> bool:
        """プロセスのcwdを使わず、同じ出力と明示された起点で内外を判定する。"""
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
    """一覧と出力に記録されたrootdirから位置候補の根拠を集める。"""
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
    """同じ実行に由来する根拠を持つファイル位置を返す。"""
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
            # tb=lineの捕捉本文は位置行に先行する。次の実位置かE行で通常の解析へ戻る。
            if captured and not (paths.known(file) or paths.exists(file) or "." in file or "/" in file or "\\" in file):
                continue
            # ファイル名にも数値付きのコロンが入り得るため、根拠のある境界へ固定して照合する。
            exact = pattern.pattern.replace(r"(?P<file>\S.*?)", rf"(?P<file>{re.escape(file)})", 1)
            match = re.compile(exact, pattern.flags).match(output, line.start())
            if match is not None:
                captured = False
                yield match
                break


def pytest_block_paths(
    paths: PytestPaths, summary: dict[tuple[str, str], str | None], test_name: str, block: str, *, doctest: bool
) -> PytestPaths:
    """拡張名の所有者を失敗テストへ限定し、そのテストのトレースバックを確認する。"""
    owned = {file for file, test in summary if test == test_name}
    if doctest:
        return dataclasses.replace(paths, known_files=owned | {test_name}, allow_expanded=True)
    function = test_name.split("[", 1)[0].rsplit(".", 1)[-1]
    trace = block.lstrip("\r\n")
    trace = re.sub(r"^\[gw[^\]]+\] [^\n]*\n", "", trace, count=1).lstrip("\r\n")
    error = re.search(r"^E[ \t]+", trace, re.MULTILINE)
    if error is None:
        return dataclasses.replace(paths, known_files=owned, allow_expanded=False)
    # 例外本文を除いた先頭エントリーだけで、フレームとソースの組を認識する。
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
        # xdistの-rNは一覧もファイル単位の進捗も省く。対象関数のソース直後の
        # 最初の位置行を、そのブロック自身のファイルとして対応付ける。
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
    """捕捉出力の節より前にあるトレースバックを返す。"""
    section = PYTEST_CAPTURED_SECTION_RE.search(block)
    return block[: section.start()] if section is not None else block


ANSI_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


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
    `pyfltr.parsing.pytest_nested.pytest_parent_summary_start`で親自身の失敗一覧の見出しを特定し、対象の見出し以降のみを
    渡す）。テストの捕捉出力に子プロセスpytestの`FAILED ...`行が混入していても、
    実在しない失敗として誤検出しないため。
    """
    summary: dict[tuple[str, str], str | None] = {}
    for match in PYTEST_SUMMARY_RE.finditer(output):
        file_path = pyfltr.paths.normalize_separators(match.group("file"))
        test_name = match.group("test").replace("::", ".")
        raw_message = match.group("message")
        summary[(file_path, test_name)] = raw_message.strip() if raw_message is not None else None
    return summary


def parse_pytest_error_summary(output: str) -> dict[tuple[str, str], str | None]:
    """`short test summary info`の`ERROR <file>[::<test>] [- <message>]`行を解析する。

    キーは`(file, test)`で、`test`は`parse_pytest_summary`と同じく`.`区切りへ正規化する。
    収集エラーは試験名を持たないため`test`を空文字列とする。
    同じ試験に本体の失敗（`FAILED`）と後始末のERRORが重なる場合も別々に扱えるよう、
    `FAILED`行とは別の辞書へ集める。走査範囲は呼び出し側が親自身の失敗一覧以降へ限る。
    失敗一覧の見出しを持たない出力では、ログ捕捉（`ERROR    <logger>:...`）の行と取り違えるため呼ばない。
    """
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


def select_pytest_location_line(block: str, *, allow_fallback: bool, paths: PytestPaths | None = None) -> re.Match[str] | None:
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
    """位置行に対応するエラー行（`E   <message>`）の本文を返す。

    例外の連鎖では複数のエントリーがブロック内に並ぶ。直前の位置行より後にある最初の
    エラー行が対象のエントリーの例外を表すため、そこから採る。直前の位置行が無い場合
    （エントリーが1つだけの場合）はブロック先頭から探す。
    """
    previous_end = 0
    for match in pytest_positions(PYTEST_LOCATION_LINE_RE, block[: location.start()], paths):
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
