import json
import pathlib
import sys

OUTPUT_FORMATS: tuple[str, ...] = ("text", "json", "jsonl")
VALID_OUTPUT_FORMATS: frozenset[str] = frozenset(OUTPUT_FORMATS)


def print_json(
    payload: object,
    output_file: pathlib.Path | None = None,
    *,
    indent: int | None = None,
    compact: bool = False,
) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=indent, separators=(",", ":") if compact else None)
    if output_file is not None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
