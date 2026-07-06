"""pyfltr.colloquial.check のテストコード。

load_patterns / scan_text / first_hit / mask_allowed / mask_blockquote_lines / mask_fenced_code_blocksの検証。
テスト本体に口語表現を直接書かないため、辞書ファイルから動的にサンプルを構築する。
"""

import pathlib
import re

import pytest

import pyfltr.colloquial.check

_PatternList = list[tuple[re.Pattern[str], str | None]]


def _expand_pattern(pattern_str: str) -> str:
    """辞書パターンから自己マッチサンプルを生成する簡易展開。

    辞書のパターン記法で使われる構造を順に単純化する。
    `[...]`を先頭文字へ、`(A|B|...)`を先頭選択肢へ置換し、
    エスケープされていない`?`・`*`を除去してオプション要素を保持する。
    """
    s = re.sub(r"\(\?(?:<?[=!]|:)[^)]*\)", "", pattern_str)
    s = re.sub(r"\[([^\]]+)\]", lambda m: m.group(1)[0], s)
    s = re.sub(r"\(([^)]+)\)", lambda m: m.group(1).split("|")[0], s)
    s = re.sub(r"(?<!\\)[?*]", "", s)
    return s


def _read_patterns_text(path: pathlib.Path) -> list[str]:
    """ファイルから（コンパイル前の）正規表現文字列を順に取り出す。

    タブ区切りで併記された置換候補列は除外し、パターン部のみを返す。
    """
    rows: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        head = stripped.split("\t", 1)[0]
        if head:
            rows.append(head)
    return rows


@pytest.fixture(name="deny_patterns", scope="module")
def _deny_patterns() -> _PatternList:
    return pyfltr.colloquial.check.load_patterns(pyfltr.colloquial.check.DENY_PATH)


@pytest.fixture(name="allow_patterns", scope="module")
def _allow_patterns() -> _PatternList:
    return pyfltr.colloquial.check.load_patterns(pyfltr.colloquial.check.ALLOW_PATH)


@pytest.fixture(name="overlap_sample", scope="module")
def _overlap_sample(deny_patterns: _PatternList) -> tuple[str, str]:
    """allowlist側のパターンを展開して、denylist側にも当たる最初のサンプルを返す。

    戻り値は`(allow_sample, deny_substring)`のタプル。
    マスキング動作の検証用に、両者を共通の文字列から取り出す。
    """
    for raw in _read_patterns_text(pyfltr.colloquial.check.ALLOW_PATH):
        sample = _expand_pattern(raw)
        for dp, _ in deny_patterns:
            m = dp.search(sample)
            if m:
                return sample, m.group(0)
    pytest.skip("allowlistとdenylistが重複するサンプルが無く、マスキングを検証できない")
    return "", ""  # 到達不能（`pytest.skip`で関数は終了する）


class TestLoadPatterns:
    """`load_patterns` のテスト。"""

    def test_dictionaries_are_loaded(self, deny_patterns: _PatternList, allow_patterns: _PatternList) -> None:
        assert deny_patterns, "denylist 辞書が空"
        assert allow_patterns, "allowlist 辞書が空"

    def test_skips_comments_and_blanks(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "p.txt"
        f.write_text("# header\n\n[xy]\n   \n# tail\n", encoding="utf-8")
        patterns = pyfltr.colloquial.check.load_patterns(f)
        assert len(patterns) == 1
        compiled, replacement = patterns[0]
        assert compiled.search("x")
        assert replacement is None

    def test_skips_invalid_regex(self, tmp_path: pathlib.Path) -> None:
        f = tmp_path / "p.txt"
        f.write_text("[unclosed\n", encoding="utf-8")
        assert not pyfltr.colloquial.check.load_patterns(f)

    def test_missing_file_returns_empty(self, tmp_path: pathlib.Path) -> None:
        assert not pyfltr.colloquial.check.load_patterns(tmp_path / "missing.txt")

    @pytest.mark.parametrize(
        ("line", "expected_replacement"),
        [
            ("[xy]", None),  # タブ無し
            ("[xy]\t候補", "候補"),  # 候補あり
            ("[xy]\t", None),  # タブ末尾のみ（空のreplacement）
            ("[xy]\t候補\t補足", "候補\t補足"),  # 複数タブ: 最初のタブまでがパターン
        ],
    )
    def test_parses_replacement_column(self, tmp_path: pathlib.Path, line: str, expected_replacement: str | None) -> None:
        f = tmp_path / "p.txt"
        f.write_text(f"{line}\n", encoding="utf-8")
        patterns = pyfltr.colloquial.check.load_patterns(f)
        assert len(patterns) == 1
        compiled, replacement = patterns[0]
        assert compiled.search("x")
        assert replacement == expected_replacement


class TestFirstHit:
    """`first_hit` のテスト。"""

    def test_detects_isolated_deny(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        _, deny_sub = overlap_sample
        text = f"概要は{deny_sub}該当する。"
        assert pyfltr.colloquial.check.first_hit(text, deny_patterns, allow_patterns)

    def test_swallowed_by_allow(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        allow_sample, _ = overlap_sample
        text = f"概要は{allow_sample}該当する。"
        assert not pyfltr.colloquial.check.first_hit(text, deny_patterns, allow_patterns)

    def test_clean_text(self, deny_patterns: _PatternList, allow_patterns: _PatternList) -> None:
        text = "plain ASCII content without Japanese characters.\n"
        assert not pyfltr.colloquial.check.first_hit(text, deny_patterns, allow_patterns)

    def test_empty_deny_returns_false(self, allow_patterns: _PatternList) -> None:
        text = "全てのパターンが未登録なら検出は発生しない"
        assert not pyfltr.colloquial.check.first_hit(text, [], allow_patterns)


class TestScanText:
    """`scan_text` のテスト。"""

    def test_returns_position_for_match(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        _, deny_sub = overlap_sample
        text = f"line1\n本文に{deny_sub}末尾\nline3"
        hits = pyfltr.colloquial.check.scan_text(text, deny_patterns, allow_patterns)
        assert hits, "検出が無い"
        line_no, col, match_str, snippet, _ = hits[0]
        assert line_no == 2
        assert col >= 1
        assert match_str
        assert "末尾" in snippet

    def test_empty_for_clean_text(self, deny_patterns: _PatternList, allow_patterns: _PatternList) -> None:
        assert not pyfltr.colloquial.check.scan_text("nothing here.\n", deny_patterns, allow_patterns)

    def test_empty_when_no_deny(self, allow_patterns: _PatternList) -> None:
        assert not pyfltr.colloquial.check.scan_text("様々な内容の文字列", [], allow_patterns)

    @pytest.mark.parametrize("raw_pattern", _read_patterns_text(pyfltr.colloquial.check.DENY_PATH))
    def test_every_deny_entry_self_matches(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, raw_pattern: str
    ) -> None:
        """denylist各エントリが自身の展開サンプルで必ず検出される。

        辞書再編・文字クラス展開規則の変更で当該パターンが意図せず無効化されても
        本テストが回帰を検出する。
        """
        sample = _expand_pattern(raw_pattern)
        if pyfltr.colloquial.check.mask_allowed(sample, allow_patterns) != sample:
            pytest.skip(f"サンプルがallowlistによりマスクされた: {raw_pattern}")
        text = f"。{sample}。"
        assert pyfltr.colloquial.check.scan_text(text, deny_patterns, allow_patterns)

    @pytest.mark.parametrize(
        ("dict_line", "expected_replacement"),
        [
            ("[xy]", None),
            ("[xy]\t候補", "候補"),
        ],
    )
    def test_returns_replacement(self, tmp_path: pathlib.Path, dict_line: str, expected_replacement: str | None) -> None:
        f = tmp_path / "p.txt"
        f.write_text(f"{dict_line}\n", encoding="utf-8")
        deny = pyfltr.colloquial.check.load_patterns(f)
        hits = pyfltr.colloquial.check.scan_text("abc x def", deny, [])
        assert hits
        _, _, _, _, replacement = hits[0]
        assert replacement == expected_replacement

    @pytest.mark.parametrize("raw_pattern", _read_patterns_text(pyfltr.colloquial.check.ALLOW_PATH))
    def test_every_allow_entry_masks_deny_overlap(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, raw_pattern: str
    ) -> None:
        """denylistと重複するallowlistエントリの自己サンプルは検出されない。

        `words_allow.txt`冒頭コメントが定める「denylist側にマッチしつつ本ファイルでマスクされる文脈」の
        実装を検証する。allowlistなしでdenylist検出が発火するサンプルに限定し、
        allowlist適用後は検出されないことを確認する。
        """
        sample = _expand_pattern(raw_pattern)
        if not re.compile(raw_pattern).search(sample):
            pytest.skip(f"サンプル生成不能: {raw_pattern}")
        if not pyfltr.colloquial.check.first_hit(sample, deny_patterns, []):
            pytest.skip(f"denylistと重複しないエントリ: {raw_pattern}")
        assert not pyfltr.colloquial.check.first_hit(sample, deny_patterns, allow_patterns)


class TestMaskAllowed:
    """`mask_allowed` のテスト。"""

    def test_preserves_length(self, allow_patterns: _PatternList, overlap_sample: tuple[str, str]) -> None:
        allow_sample, _ = overlap_sample
        text = f"abc{allow_sample}xyz"
        masked = pyfltr.colloquial.check.mask_allowed(text, allow_patterns)
        assert len(masked) == len(text)
        assert masked != text  # 少なくとも 1 箇所はマスクされている
        assert masked.startswith("abc")
        assert masked.endswith("xyz")


class TestMaskBlockquoteLines:
    """`mask_blockquote_lines` のテスト。"""

    def test_replaces_blockquote_with_spaces(self) -> None:
        text = "本文1\n> 引用文の例示\n本文2\n"
        masked = pyfltr.colloquial.check.mask_blockquote_lines(text)
        assert len(masked) == len(text)
        # 引用行が空白化され、他行は維持される
        assert masked.splitlines() == ["本文1", " " * len("> 引用文の例示"), "本文2"]

    def test_keeps_non_blockquote_unchanged(self) -> None:
        text = "通常段落\nもう一行\n"
        assert pyfltr.colloquial.check.mask_blockquote_lines(text) == text

    def test_blockquote_without_space_after_marker(self) -> None:
        text = ">引用文\n本文\n"
        masked = pyfltr.colloquial.check.mask_blockquote_lines(text)
        assert masked.splitlines() == [" " * len(">引用文"), "本文"]

    def test_marker_in_middle_of_line_is_not_masked(self) -> None:
        text = "比較 a > b の表記\n"
        assert pyfltr.colloquial.check.mask_blockquote_lines(text) == text

    def test_empty_text(self) -> None:
        assert pyfltr.colloquial.check.mask_blockquote_lines("") == ""


class TestMaskFencedCodeBlocks:
    """`mask_fenced_code_blocks` のテスト。"""

    def test_replaces_backtick_fenced_block_inner_lines(self) -> None:
        text = "本文1\n```\n内側の文\n```\n本文2\n"
        masked = pyfltr.colloquial.check.mask_fenced_code_blocks(text)
        assert len(masked) == len(text)
        assert masked.splitlines() == ["本文1", "```", " " * len("内側の文"), "```", "本文2"]

    def test_replaces_tilde_fenced_block_inner_lines(self) -> None:
        text = "~~~\n内側\n~~~\n"
        masked = pyfltr.colloquial.check.mask_fenced_code_blocks(text)
        assert masked.splitlines() == ["~~~", " " * len("内側"), "~~~"]

    def test_close_requires_same_marker_char(self) -> None:
        # 開始がバッククォートなのでチルダでは閉じない（テキスト末尾までマスク継続）
        text = "```\n内側\n~~~\n後続\n"
        masked = pyfltr.colloquial.check.mask_fenced_code_blocks(text)
        assert masked.splitlines() == ["```", " " * len("内側"), " " * len("~~~"), " " * len("後続")]

    def test_close_allows_longer_marker(self) -> None:
        text = "```\n内側\n````\n後続\n"
        masked = pyfltr.colloquial.check.mask_fenced_code_blocks(text)
        assert masked.splitlines() == ["```", " " * len("内側"), "````", "後続"]

    def test_unclosed_fence_masks_until_end(self) -> None:
        text = "```\n内側1\n内側2\n"
        masked = pyfltr.colloquial.check.mask_fenced_code_blocks(text)
        assert masked.splitlines() == ["```", " " * len("内側1"), " " * len("内側2")]

    def test_text_without_fence_unchanged(self) -> None:
        text = "通常段落\nもう一行\n"
        assert pyfltr.colloquial.check.mask_fenced_code_blocks(text) == text

    def test_empty_text(self) -> None:
        assert pyfltr.colloquial.check.mask_fenced_code_blocks("") == ""


class TestBlockquoteSkipIntegration:
    """`scan_text` / `first_hit` での引用行スキップ統合テスト。"""

    @pytest.mark.parametrize(
        ("template", "should_hit"),
        [
            # 行頭`>`+スペースの標準的な引用行はスキップされる
            ("> {sub}\n", False),
            # `>`直後にスペースが無い引用行もスキップされる
            (">{sub}\n", False),
            # 引用行直下の非引用行は通常通り検出される
            ("> 別文\n本文に{sub}該当\n", True),
            # `>`が行頭以外にある行は引用ブロックではないため検出対象
            ("文中の > {sub}該当\n", True),
        ],
    )
    def test_first_hit_blockquote_handling(
        self,
        deny_patterns: _PatternList,
        allow_patterns: _PatternList,
        overlap_sample: tuple[str, str],
        template: str,
        should_hit: bool,
    ) -> None:
        _, deny_sub = overlap_sample
        text = template.format(sub=deny_sub)
        assert pyfltr.colloquial.check.first_hit(text, deny_patterns, allow_patterns) is should_hit

    def test_scan_text_skips_blockquote_line(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        _, deny_sub = overlap_sample
        text = f"> {deny_sub}該当\n"
        assert not pyfltr.colloquial.check.scan_text(text, deny_patterns, allow_patterns)

    def test_scan_text_detects_following_non_blockquote(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        _, deny_sub = overlap_sample
        text = f"> 引用部の{deny_sub}該当\n本文の{deny_sub}該当\n"
        hits = pyfltr.colloquial.check.scan_text(text, deny_patterns, allow_patterns)
        assert hits, "本文側の検出が必要"
        # 引用行（1行目）はスキップされ、本文（2行目）のみ検出される
        assert all(line_no == 2 for line_no, _, _, _, _ in hits)


class TestFencedCodeSkipIntegration:
    """`scan_text` / `first_hit` でのフェンス付きコードブロックスキップ統合テスト。"""

    def test_first_hit_skips_inside_fence(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        _, deny_sub = overlap_sample
        text = f"```\n{deny_sub}該当\n```\n"
        assert pyfltr.colloquial.check.first_hit(text, deny_patterns, allow_patterns) is False

    def test_scan_text_skips_inside_fence(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        _, deny_sub = overlap_sample
        text = f"```\n{deny_sub}該当\n```\n"
        assert not pyfltr.colloquial.check.scan_text(text, deny_patterns, allow_patterns)

    def test_scan_text_detects_outside_fence(
        self, deny_patterns: _PatternList, allow_patterns: _PatternList, overlap_sample: tuple[str, str]
    ) -> None:
        _, deny_sub = overlap_sample
        text = f"```\nフェンス内の{deny_sub}該当\n```\n本文の{deny_sub}該当\n"
        hits = pyfltr.colloquial.check.scan_text(text, deny_patterns, allow_patterns)
        assert hits, "フェンス外側の検出が必要"
        # フェンス内（2行目）はスキップされ、本文（4行目）のみ検出される
        assert all(line_no == 4 for line_no, _, _, _, _ in hits)
