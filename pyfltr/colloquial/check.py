r"""口語表現検査の純粋ロジック。

denylist・allowlistの正規表現を辞書ファイルから読み込み、
Markdownの引用ブロック・フェンス付きコードブロック・allowlist該当箇所を
同長空白マスクで除外したうえでdenylistに一致する箇所を列挙する。

denylistの内容をソースコード（`.py`ファイル）へ静的に埋め込まない設計のため、
パターンを辞書ファイルから動的に読み込む。
コーディングエージェントがソースコードを読む際にdenylist内容がコンテキストへ
混入しないようにする狙いがある。

辞書ファイルの各行は`pattern`または`pattern\treplacement`形式（タブ区切り）。
タブを含まない行は置換候補なし扱いとし、CLI出力にも候補は表示されない。
"""

import pathlib
import re

_DICT_DIR = pathlib.Path(__file__).resolve().parent
DENY_PATH = _DICT_DIR / "words.txt"
ALLOW_PATH = _DICT_DIR / "words_allow.txt"


_KANJI_CLASS = r"[一-鿿]"
_KANJI_LEFT_BOUNDARY = rf"(?<!{_KANJI_CLASS})"
# 漢字1文字の語幹に活用形の文字クラスが続く形。この形だけが漢語複合語の接尾と衝突する。
_SINGLE_KANJI_STEM_RE = re.compile(rf"\A{_KANJI_CLASS}\[")


def _with_kanji_left_boundary(pattern: str) -> str:
    """漢字1文字の語幹で始まるパターンへ、直前が漢字である位置での一致を禁じる条件を前置する。

    denylistのパターンは語境界を持たない部分一致で適用される。
    語幹が漢字1文字の場合、当該漢字を末尾に持つ漢語複合語の内部でも一致が成立する。
    条件を行ごとに手書きさせると書き漏らしが辞書へ混入するため、読込時に一律で与える。

    対象を漢字1文字＋活用形の文字クラスの形に限るのは、
    語幹が2文字以上の漢字である場合は、当該語幹が別の漢語複合語の接尾になりにくく、
    かつ接頭辞が直前に付く形で検出すべき真陽性が存在するためである。

    既に否定先読み・否定後読みで始まる行は、記述者が境界条件を明示した行として扱い変更しない。
    """
    if pattern.startswith(("(?<!", "(?!")):
        return pattern
    if _SINGLE_KANJI_STEM_RE.match(pattern) is None:
        return pattern
    return _KANJI_LEFT_BOUNDARY + pattern


def load_patterns(path: pathlib.Path, *, kanji_left_boundary: bool = False) -> list[tuple[re.Pattern[str], str | None]]:
    r"""辞書ファイルから1行1正規表現を読み込んでコンパイルする。

    各行は`pattern`または`pattern\treplacement`形式。タブが含まれる場合は
    最初のタブまでをパターン、以降をそのままreplacementとして保持する。
    タブが無い行はreplacementを`None`として返す。

    `#`で始まる行と空行は無視する。
    不正な正規表現はチェッカーを破損させないためスキップする。

    `kanji_left_boundary`が真の場合、漢字1文字の語幹で始まるパターンへ左境界条件を前置する
    （対象の判定は`_with_kanji_left_boundary`が持つ）。
    denylistの読み込みでのみ真を指定する。allowlistは漢語複合語の側を一致させる役割を
    持つため、同じ条件を与えると当該役割が成立しない。
    """
    if not path.is_file():
        return []
    patterns: list[tuple[re.Pattern[str], str | None]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # 最初のタブまでをパターン、残りを置換候補として扱う。
        # `strip()`で末尾の空白文字（タブ含む）は除去済みのため、タブが無い行は`replacement=None`になる。
        head, sep, tail = stripped.partition("\t")
        replacement = tail if sep else None
        if kanji_left_boundary:
            head = _with_kanji_left_boundary(head)
        try:
            patterns.append((re.compile(head), replacement))
        except re.error:
            continue
    return patterns


def mask_allowed(text: str, allow_patterns: list[tuple[re.Pattern[str], str | None]]) -> str:
    """allow_patternsに一致する部分を同長の空白で置き換える。

    空文字ではなく空白で埋めることで位置情報を保持する。
    後続のdenylist検索結果が元テキスト上のオフセットと整合する。
    """
    masked = text
    for ap, _ in allow_patterns:
        masked = ap.sub(lambda m: " " * len(m.group(0)), masked)
    return masked


_BLOCKQUOTE_LINE_RE = re.compile(r"(?m)^>.*$")


def mask_blockquote_lines(text: str) -> str:
    """Markdown引用ブロック（行頭`>`で始まる行）を同長の空白で置き換える。

    ユーザー提示素材の原文転記が口語表現として誤検出される事態を避ける。
    `mask_allowed`と同じ同長空白置換方式で、検出箇所のオフセット・行番号計算を変更しない。
    """
    return _BLOCKQUOTE_LINE_RE.sub(lambda m: " " * len(m.group(0)), text)


_FENCE_LINE_RE = re.compile(r"^(`{3,}|~{3,})")


def mask_fenced_code_blocks(text: str) -> str:
    """Markdownフェンス付きコードブロック内の行を同長の空白で置き換える。

    `mask_blockquote_lines`と同じく、ユーザー提示素材の原文転記が口語表現として
    誤検出される事態を避ける。開始フェンスは行頭の三連以上の連続バッククォート
    または連続チルダ。終了フェンスは開始と同種かつ同長以上の連続マーカー行。
    開閉フェンス行自体は維持し、内側の行のみ同長空白へ置換する。
    オフセット・行番号は変更しない。
    """
    out: list[str] = []
    in_fence = False
    fence_char = ""
    fence_len = 0
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\n").rstrip("\r")
        tail = line[len(body) :]
        if not in_fence:
            m = _FENCE_LINE_RE.match(body)
            if m:
                marker = m.group(1)
                in_fence = True
                fence_char = marker[0]
                fence_len = len(marker)
            out.append(line)
            continue
        close = re.match(rf"^{re.escape(fence_char)}{{{fence_len},}}\s*$", body)
        if close:
            in_fence = False
            fence_char = ""
            fence_len = 0
            out.append(line)
        else:
            out.append(" " * len(body) + tail)
    return "".join(out)


def _mask_all(text: str, allow_patterns: list[tuple[re.Pattern[str], str | None]]) -> str:
    """引用ブロック・フェンス付きコードブロック・allowlist該当箇所を順にマスクする。

    `scan_text`と`first_hit`で共通の前処理として使う。
    """
    return mask_allowed(mask_fenced_code_blocks(mask_blockquote_lines(text)), allow_patterns)


def scan_text(
    text: str,
    deny_patterns: list[tuple[re.Pattern[str], str | None]],
    allow_patterns: list[tuple[re.Pattern[str], str | None]],
) -> list[tuple[int, int, str, str, str | None]]:
    """テキスト全体を検査して検出箇所のリストを返す。

    各要素は`(行番号, 列, 検出文字列, 行抜粋, 置換候補)`のタプル。
    置換候補は辞書ファイルでパターンに併記されたreplacement列を返す。候補なしは`None`。
    allow_patternsマスク後もオフセットを維持しているため、元テキスト上の正確な位置を指す。
    """
    if not deny_patterns:
        return []
    masked = _mask_all(text, allow_patterns)
    hits: list[tuple[int, int, str, str, str | None]] = []
    for dp, replacement in deny_patterns:
        for m in dp.finditer(masked):
            line_start = text.rfind("\n", 0, m.start()) + 1
            line_no = text[: m.start()].count("\n") + 1
            col = m.start() - line_start + 1
            line_end = text.find("\n", m.end())
            if line_end == -1:
                line_end = len(text)
            snippet = text[line_start:line_end].rstrip()
            hits.append((line_no, col, m.group(0), snippet, replacement))
    hits.sort(key=lambda h: (h[0], h[1]))
    return hits


def first_hit(
    text: str,
    deny_patterns: list[tuple[re.Pattern[str], str | None]],
    allow_patterns: list[tuple[re.Pattern[str], str | None]],
) -> bool:
    """検出が1件でもあれば真を返す（呼び出し元の高速判定用の経路）。"""
    if not deny_patterns:
        return False
    masked = _mask_all(text, allow_patterns)
    return any(dp.search(masked) for dp, _ in deny_patterns)
