"""grepの全検索結果からLLM向けの出力方式を選択する。"""

from __future__ import annotations

import dataclasses
import json
import typing

OutputMode = typing.Literal["full", "mixed", "grouped", "counts", "sampled"]
OutputFormat = typing.Literal["text", "json", "jsonl", "mcp"]

DEFAULT_RESULT_BUDGET = 10_000


@dataclasses.dataclass(frozen=True)
class Selection:
    """選択済みの検索結果と省略量。"""

    output_mode: OutputMode
    matches: list[dict[str, typing.Any]]
    file_results: list[dict[str, typing.Any]]
    total_matches: int
    files_with_matches: int
    returned_matches: int
    omitted_matches: int
    omitted_files: int


def select_output(
    matches: list[dict[str, typing.Any]],
    *,
    output_format: OutputFormat,
    budget: int = DEFAULT_RESULT_BUDGET,
) -> Selection:
    """実際の結果部分の直列化文字数を比較し、情報量が最大の候補を返す。"""
    groups = _group_matches(matches)
    full_length = serialized_length("full", matches, [], output_format=output_format)
    if full_length <= budget:
        return _selection("full", matches, [], groups)

    grouped_full = [_file_result(file, group, count=len(group)) for file, group in groups]
    grouped_length = serialized_length("mixed", [], grouped_full, output_format=output_format)
    if grouped_length <= budget and grouped_length <= full_length:
        return _selection("mixed", [], grouped_full, groups)

    savings: list[tuple[int, int]] = []
    for index, (file, group) in enumerate(groups):
        full_item = _file_result(file, group, count=len(group))
        count_item = _file_result(file, [], count=len(group))
        full_item_length = serialized_length("mixed", [], [full_item], output_format=output_format)
        count_item_length = serialized_length("mixed", [], [count_item], output_format=output_format)
        savings.append((full_item_length - count_item_length, index))
    savings.sort(key=lambda item: (-item[0], item[1]))

    mixed = list(grouped_full)
    collapsed: set[int] = set()
    for saving, index in savings:
        if saving <= 0:
            continue
        mixed[index] = _file_result(groups[index][0], [], count=len(groups[index][1]))
        collapsed.add(index)
        mixed_length = serialized_length("mixed", [], mixed, output_format=output_format)
        if mixed_length <= budget and mixed_length <= full_length and len(collapsed) < len(groups):
            return _selection("mixed", [], mixed, groups)

    grouped = _representative_file_results(groups, output_format=output_format, budget=budget)
    if grouped is not None:
        grouped_length = serialized_length("grouped", [], grouped, output_format=output_format)
        if grouped_length <= full_length:
            return _selection("grouped", [], grouped, groups)

    counts = [_file_result(file, [], count=len(group)) for file, group in groups]
    counts_length = serialized_length("counts", [], counts, output_format=output_format)
    if counts_length <= budget and counts_length <= full_length:
        return _selection("counts", [], counts, groups)

    sampled = _sample_matches(groups, output_format=output_format, budget=budget)
    return _selection("sampled", sampled, [], groups)


def full_output(matches: list[dict[str, typing.Any]]) -> Selection:
    """明示指定又は通常CLI向けの全件出力を返す。"""
    return _selection("full", matches, [], _group_matches(matches))


def serialized_length(
    mode: OutputMode,
    matches: list[dict[str, typing.Any]],
    file_results: list[dict[str, typing.Any]],
    *,
    output_format: OutputFormat,
) -> int:
    """候補を実際の結果形式へ直列化した文字数を返す。"""
    if output_format == "jsonl":
        records = _jsonl_records(mode, matches, file_results)
        return sum(len(json.dumps(record, ensure_ascii=False, separators=(",", ":"))) + 1 for record in records)
    if output_format == "text":
        return sum(len(line) + 1 for line in _text_lines(mode, matches, file_results))
    payload = {"matches": matches, "file_results": file_results}
    if output_format == "json":
        return len(json.dumps(payload, ensure_ascii=False, indent=2)) + 1
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def jsonl_result_records(selection: Selection) -> list[dict[str, typing.Any]]:
    """選択結果をJSONLの結果レコードへ変換する。"""
    return _jsonl_records(selection.output_mode, selection.matches, selection.file_results)


def text_result_lines(selection: Selection) -> list[str]:
    """選択結果をtext形式の結果行へ変換する。"""
    return _text_lines(selection.output_mode, selection.matches, selection.file_results)


def _group_matches(
    matches: list[dict[str, typing.Any]],
) -> list[tuple[str, list[dict[str, typing.Any]]]]:
    grouped: dict[str, list[dict[str, typing.Any]]] = {}
    for match in matches:
        file = typing.cast(str, match["file"])
        grouped.setdefault(file, []).append({key: value for key, value in match.items() if key != "file"})
    return list(grouped.items())


def _file_result(
    file: str,
    matches: list[dict[str, typing.Any]],
    *,
    count: int,
) -> dict[str, typing.Any]:
    result: dict[str, typing.Any] = {"file": file, "count": count}
    if matches:
        result["matches"] = list(matches)
    return result


def _representative_file_results(
    groups: list[tuple[str, list[dict[str, typing.Any]]]],
    *,
    output_format: OutputFormat,
    budget: int,
) -> list[dict[str, typing.Any]] | None:
    if not groups:
        return []
    selected = [_file_result(file, group[:1], count=len(group)) for file, group in groups]
    if serialized_length("grouped", [], selected, output_format=output_format) > budget:
        return None
    next_indexes = [1] * len(groups)
    while True:
        added = False
        for group_index, (file, group) in enumerate(groups):
            next_index = next_indexes[group_index]
            if next_index >= len(group):
                continue
            candidate = list(selected)
            candidate[group_index] = _file_result(file, group[: next_index + 1], count=len(group))
            if serialized_length("grouped", [], candidate, output_format=output_format) <= budget:
                selected = candidate
                next_indexes[group_index] += 1
                added = True
        if not added:
            return selected


def _sample_matches(
    groups: list[tuple[str, list[dict[str, typing.Any]]]],
    *,
    output_format: OutputFormat,
    budget: int,
) -> list[dict[str, typing.Any]]:
    selected: list[dict[str, typing.Any]] = []
    depth = 0
    while True:
        added = False
        for file, group in groups:
            if depth >= len(group):
                continue
            candidate_match = {"file": file, **group[depth]}
            candidate = [*selected, candidate_match]
            if serialized_length("sampled", candidate, [], output_format=output_format) <= budget:
                selected = candidate
                added = True
            elif not selected:
                return [candidate_match]
        if not added:
            return selected
        depth += 1


def _selection(
    mode: OutputMode,
    matches: list[dict[str, typing.Any]],
    file_results: list[dict[str, typing.Any]],
    groups: list[tuple[str, list[dict[str, typing.Any]]]],
) -> Selection:
    total = sum(len(group) for _file, group in groups)
    returned = len(matches) + sum(len(result.get("matches", [])) for result in file_results)
    represented_files = {typing.cast(str, match["file"]) for match in matches}
    represented_files.update(typing.cast(str, result["file"]) for result in file_results)
    return Selection(
        output_mode=mode,
        matches=matches,
        file_results=file_results,
        total_matches=total,
        files_with_matches=len(groups),
        returned_matches=returned,
        omitted_matches=total - returned,
        omitted_files=len(groups) - len(represented_files),
    )


def _jsonl_records(
    mode: OutputMode,
    matches: list[dict[str, typing.Any]],
    file_results: list[dict[str, typing.Any]],
) -> list[dict[str, typing.Any]]:
    if mode in ("full", "sampled"):
        return [{"kind": "match", **match} for match in matches]
    return [{"kind": "file", **result} for result in file_results]


def _text_lines(
    mode: OutputMode,
    matches: list[dict[str, typing.Any]],
    file_results: list[dict[str, typing.Any]],
) -> list[str]:
    if mode in ("full", "sampled"):
        return [_format_match(match) for match in matches]
    lines: list[str] = []
    for result in file_results:
        file = typing.cast(str, result["file"])
        nested = typing.cast(list[dict[str, typing.Any]], result.get("matches", []))
        if not nested:
            lines.append(f"{file}:{result['count']} match(es) (details omitted)")
            continue
        if mode == "grouped":
            lines.append(f"{file}:{result['count']} match(es) ({len(nested)} representative match(es) shown)")
        lines.extend(_format_match({"file": file, **match}) for match in nested)
    return lines


def _format_match(match: dict[str, typing.Any]) -> str:
    offset = int(match.get("line_text_offset", 0))
    prefix = f"[+{offset}] " if "line_text" in match.get("truncated", []) and offset else ""
    return f"{match['file']}:{match['line']}:{match['col']}:{prefix}{match['line_text']}"
