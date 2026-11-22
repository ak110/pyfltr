import markdown_it
import markdown_it.token
import markdown_it.utils
import pytest

from pyfltr.markdown_structure import check


def test_independent_blocks_do_not_require_full_parse_per_break(monkeypatch: pytest.MonkeyPatch) -> None:
    source = ("甲。乙。\n\n- 丙。丁。\n\n") * 200
    parsed_lengths: list[int] = []
    original_parse = markdown_it.MarkdownIt.parse

    def count_parse(
        parser: markdown_it.MarkdownIt, text: str, env: markdown_it.utils.EnvType | None = None
    ) -> list[markdown_it.token.Token]:
        parsed_lengths.append(len(text))
        return original_parse(parser, text, env)

    monkeypatch.setattr(markdown_it.MarkdownIt, "parse", count_parse)
    issues, insertions = check.inspect(source)
    assert len(issues) == len(insertions) == 400
    assert sum(parsed_lengths) < 6 * len(source)
