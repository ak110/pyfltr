"""pytestの設定競合の検出。"""

import re

import pyfltr.parsing.pytest_common
import pyfltr.parsing.pytest_nested

PYTEST_CONFIG_CONFLICT_RE = re.compile(
    r"^configfile: (?P<used>.+?) \(WARNING: ignoring pytest config in (?P<ignored>.+?)!\)\s*$"
)


PYTEST_COLLECTION_RE = re.compile(r"^(?:collecting |collected \d+ items?)")


PYTEST_SECTION_RE = re.compile(r"^=+ .+ =+$")


def is_pytest_header_end(line: str) -> bool:
    """与えられた行がpytestのヘッダー領域の終端に当たるかを判定する。

    ヘッダー領域は実行開始行から始まり、収集開始行・節見出し・入れ子実行の標識のいずれかで終わる。
    収集開始行だけを終端とすると、対象の行を出力しない構成でヘッダー領域が閉じない。
    pytest-xdistは収集開始行の代わりに`created: N/M workers`と`N workers [M items]`を出力するため、
    実際に生じる構成である。閉じないまま走査を続けると、捕捉出力へ混入した子プロセスの
    `rootdir:`と`configfile:`の対を親のヘッダーとして読み、子側の競合を親の競合として報告する。
    """
    return (
        PYTEST_COLLECTION_RE.match(line) is not None
        or PYTEST_SECTION_RE.fullmatch(line) is not None
        or pyfltr.parsing.pytest_nested.is_pytest_nested_run_marker(line)
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

    開始・終了の対応で判定した子の区間を探索前に除く。
    開始行を持たない子（`-q`）と、ヘッダーの無い親が子の終了直後に打ち切られた出力は
    判別できず、残ったヘッダーを親のものとして扱う。
    """
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
            # pytestは`rootdir:`と`configfile:`を同一のヘッダー行群として隣接出力する。
            # 直前行での限定により、ヘッダー領域へ紛れ込んだ同形の行を競合と誤認しない。
            previous_line = line
            continue
        used = match.group("used")
        ignored = match.group("ignored")
        return f"pytest: 設定ファイルが競合しています。採用: {used} / 無視: {ignored}"
    return None
