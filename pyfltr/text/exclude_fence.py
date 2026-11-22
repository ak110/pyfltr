import pyfltr.text.fences


def mask_fenced_blocks_under_headings(text: str, headings: list[str]) -> str:
    return pyfltr.text.fences.mask_fenced_blocks(text, headings=headings)
