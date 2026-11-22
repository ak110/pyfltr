import dataclasses
import re

from pyfltr.test_copies.model import COPY_KINDS, TestFunction, combine


class _InputError(ValueError):
    def __init__(self, lineno: int, message: str) -> None:
        super().__init__(message)
        self.lineno = lineno


@dataclasses.dataclass
class _Token:
    value: str
    line: int
    literal: bool = False


def _tokens(source: str, rust: bool) -> tuple[list[_Token], dict[int, str]]:
    quoted = (
        r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\\n])\'' if rust else r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\\n])*\'|`(?:\\.|[^`\\])*`'
    )
    regex = r"(?P<regex>/(?:\\.|[^/\\\n\[]|\[(?:\\.|[^\]\\])*\])+/[a-z]*)|" if not rust else ""
    pattern = re.compile(
        r"(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|"
        r'(?P<raw>r(?P<hash>\#*)"[\s\S]*?"(?P=hash))|'
        f"(?P<string>{quoted})|" + regex + r"(?P<word>[\w$]+)|(?P<operator>=>|===|!==|==|!=|::|&&|\|\||.)",
        re.UNICODE,
    )
    tokens: list[_Token] = []
    comments: dict[int, str] = {}
    line = 1
    for match in pattern.finditer(source):
        value = match.group()
        group = match.lastgroup
        if group == "comment":
            for offset, part in enumerate(value.splitlines()):
                comments[line + offset] = part
        elif group != "space":
            if value == "/" and source[match.end() : match.end() + 1] == "*":
                raise _InputError(line, "コメントが閉じていない")
            if value in {'"', "`"} or (value == "'" and not rust):
                raise _InputError(line, "閉じていない文字列")
            tokens.append(_Token(value, line, group in {"string", "raw", "regex"} or value.isdecimal()))
        line += value.count("\n")
    return tokens, comments


def _pairs(tokens: list[_Token]) -> dict[int, int]:
    pairs: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.literal:
            continue
        if token.value in {"(", "{", "["}:
            stack.append(index)
        elif token.value in {")", "}", "]"}:
            if not stack or tokens[stack[-1]].value != {")": "(", "}": "{", "]": "["}[token.value]:
                raise _InputError(token.line, "括弧が対応していない")
            opening = stack.pop()
            pairs[opening] = index
    if stack:
        raise _InputError(tokens[stack[-1]].line, "括弧が閉じていない")
    return pairs


def _foreign_value(tokens: list[_Token], env: dict[str, str]) -> str:
    transparent = {"contains", "includes", "startsWith", "endsWith", "trim", "to_string", "as_str", "keys", "len", "unwrap"}
    readers = {"read_to_string", "readFileSync", "readFile", "include_str", "include_bytes"}
    for index, token in enumerate(tokens[:-1]):
        following = index + 1 + (tokens[index + 1].value == "!")
        if (
            following < len(tokens)
            and tokens[following].value == "("
            and token.value.isidentifier()
            and token.value not in transparent | readers | {"vec"}
        ):
            return "unknown"
    significant = [token for token in tokens if token.value not in {"(", ")", "[", "]", "{", "}", ",", ";", "&", "!"}]
    if not significant:
        return "unknown"
    kinds = []
    for index, token in enumerate(significant):
        if token.literal or token.value in {"true", "false", "null", "None", "vec"}:
            kinds.append("literal")
        elif token.value in readers:
            kinds.append("text")
        elif env.get(token.value) == "module" and index + 1 < len(significant) and significant[index + 1].value in {".", "::"}:
            continue
        elif token.value in env:
            kinds.append(env[token.value])
        elif token.value.isidentifier() and token.value.isupper():
            root = index - 2
            while root >= 2 and significant[root - 1].value in {".", "::"}:
                root -= 2
            static = (
                index > 0 and significant[index - 1].value == "::" or root >= 0 and env.get(significant[root].value) == "module"
            )
            kinds.append("constant" if static else "unknown")
        elif (
            token.value
            in {
                ".",
                "::",
                "==",
                "===",
                "!=",
                "!==",
                "in",
                ":",
                "as",
                "str",
                "contains",
                "includes",
                "startsWith",
                "endsWith",
                "trim",
                "to_string",
                "as_str",
                "keys",
                "len",
                "unwrap",
                "stdout",
                "stderr",
            }
            or index + 1 < len(significant)
            and significant[index + 1].value in {".", "::"}
        ):
            continue
        else:
            kinds.append("unknown")
    return combine(kinds)


def _foreign_checks(
    tokens: list[_Token],
    pairs: dict[int, int],
    start: int,
    end: int,
    helpers: dict[str, tuple[list[str], int, int]],
    bindings: dict[str, str] | None = None,
    stack: tuple[str, ...] = (),
) -> list[str]:
    env = dict(bindings or {})
    checks: list[str] = []
    index = start
    while index < end:
        token = tokens[index]
        if token.value in {"let", "const", "var"} and index + 2 < end:
            name_index = index + 1 + (tokens[index + 1].value == "mut")
            stop = next((position for position in range(name_index + 1, end) if tokens[position].value == ";"), end)
            equal = next((position for position in range(name_index + 1, stop) if tokens[position].value == "="), None)
            if equal is not None:
                env[tokens[name_index].value] = _foreign_value(tokens[equal + 1 : stop], env)
        if token.value == "for" and index + 3 < end:
            stop = next((position for position in range(index + 1, end) if tokens[position].value == "{"), end)
            relation = next((position for position in range(index + 1, stop) if tokens[position].value in {"in", "of"}), None)
            if relation is not None:
                name_index = index + 1 + (tokens[index + 1].value == "(")
                if tokens[name_index].value in {"const", "let"}:
                    name_index += 1
                env[tokens[name_index].value] = _foreign_value(tokens[relation + 1 : stop], env)
        if token.value in helpers and token.value not in stack and index + 1 in pairs:
            params, helper_start, helper_end = helpers[token.value]
            close = pairs[index + 1]
            arguments = [tokens[first:last] for first, last in _arguments(tokens, pairs, index + 1)]
            local = {name: _foreign_value(argument, env) for name, argument in zip(params, arguments, strict=False)}
            checks.extend(_foreign_checks(tokens, pairs, helper_start, helper_end, helpers, local, (*stack, token.value)))
            index = close
        elif token.value in {
            "assert",
            "assert_eq",
            "assert_ne",
            "debug_assert",
            "debug_assert_eq",
            "strictEqual",
            "deepStrictEqual",
            "deepEqual",
            "equal",
        }:
            opening = index + 1 + (index + 1 < end and tokens[index + 1].value == "!")
            if opening in pairs:
                content = tokens[opening + 1 : pairs[opening]]
                kind = _foreign_value(content, env)
                checks.append("text-only" if kind == "text" else "constant-only" if kind == "constant" else "unknown")
                index = pairs[opening]
        elif token.value == "expect" and index + 1 in pairs:
            opening = index + 1
            close = pairs[opening]
            content = tokens[opening + 1 : close]
            matcher_end = next((position for position in range(close + 1, end) if tokens[position].value == ";"), end)
            tail = tokens[close + 1 : matcher_end]
            kind = _foreign_value(content, env)
            if any(item.value in {"toThrow", "toHaveBeenCalled", "toHaveBeenCalledWith", "toHaveProperty"} for item in tail):
                checks.append("independent")
            elif any(item.value in {"returncode", "status"} for item in content) and any(item.value == "0" for item in tail):
                checks.append("startup")
            else:
                matcher = next((position for position in range(close + 1, matcher_end) if position in pairs), None)
                if matcher is not None:
                    expected = tokens[matcher + 1 : pairs[matcher]]
                    kind = combine([kind, _foreign_value(expected, env)])
                checks.append("text-only" if kind == "text" else "constant-only" if kind == "constant" else "unknown")
            index = close
        elif token.value.startswith("assert") and index + 1 < end:
            checks.append("unknown")
        index += 1
    return checks


def _is_rust_test(tokens: list[_Token], index: int) -> bool:
    return tokens[index].value == "#" and [item.value for item in tokens[index + 1 : index + 4]] == ["[", "test", "]"]


def _type_arguments_end(tokens: list[_Token], pairs: dict[int, int], start: int, close: int, types: bool) -> int:
    depth = 1
    index = start + 1
    while index < close:
        value = tokens[index].value
        if value == "<":
            depth += 1
        elif value == ">":
            depth -= 1
            if depth == 0:
                if types or index + 1 < close and tokens[index + 1].value in {"(", "=>"}:
                    return index + 1
                return start
        elif value == ";":
            return start
        index = pairs.get(index, index) + 1
    return start


def _next_ts_token(tokens: list[_Token], pairs: dict[int, int], index: int, close: int, types: bool = False) -> int:
    if tokens[index].value == "<":
        name = index - 1
        while name >= 2 and tokens[name - 1].value in {".", "::"}:
            name -= 2
        types = (
            types
            or index > 0
            and tokens[index - 1].value in {"::", "(", ","}
            or name > 0
            and tokens[name - 1].value in {"as", "satisfies"}
        )
        end = _type_arguments_end(tokens, pairs, index, close, types)
        if end != index:
            return end
    return pairs.get(index, index) + 1


def _argument_end(tokens: list[_Token], pairs: dict[int, int], start: int, close: int, types: bool = False) -> int:
    while start < close and tokens[start].value != ",":
        start = _next_ts_token(tokens, pairs, start, close, types)
    return start


def _arguments(tokens: list[_Token], pairs: dict[int, int], opening: int, types: bool = False) -> list[tuple[int, int]]:
    close = pairs[opening]
    spans: list[tuple[int, int]] = []
    start = opening + 1
    while start < close:
        end = _argument_end(tokens, pairs, start, close, types)
        spans.append((start, end))
        start = end + 1
    return spans


def _ts_test_body(tokens: list[_Token], pairs: dict[int, int], call: int) -> tuple[int, int] | None:
    close = pairs[call]
    start = _argument_end(tokens, pairs, call + 1, close) + 1
    end = _argument_end(tokens, pairs, start, close)
    while start < end and tokens[start].value == "(" and pairs.get(start) == end - 1:
        start += 1
        end -= 1
    while start < end:
        value = tokens[start].value
        if value == "=>":
            body = start + 1
            if body == end or tokens[body].value == ";":
                raise _InputError(tokens[start].line, "試験本文がない")
            if tokens[body].value == "{":
                return body + 1, pairs[body]
            return body, end
        if value == "function":
            opening = next((position for position in range(start + 1, end) if tokens[position].value == "{"), None)
            if opening is None:
                raise _InputError(tokens[start].line, "試験本文を解析できない")
            return opening + 1, pairs[opening]
        start = _next_ts_token(tokens, pairs, start, end)
    return None


def analyze_foreign(source: str, rust: bool) -> tuple[list[TestFunction], dict[int, str]]:
    if rust and not re.search(r"#\s*\[\s*test\s*\]", source):
        return [], {}
    tokens, comments = _tokens(source, rust)
    if rust and not any(_is_rust_test(tokens, index) for index in range(len(tokens))):
        return [], {}
    pairs = _pairs(tokens)
    globals_: dict[str, str] = {}
    for index, token in enumerate(tokens):
        if token.value in {"import", "use"}:
            end = next((position for position in range(index + 1, len(tokens)) if tokens[position].value == ";"), len(tokens))
            for item in tokens[index + 1 : end]:
                if item.value.isidentifier() and item.value not in {"as", "from", "type"}:
                    globals_[item.value] = "constant" if item.value.isupper() else "module"
    helpers: dict[str, tuple[list[str], int, int]] = {}
    for index, token in enumerate(tokens):
        if token.value not in {"fn", "function"} or index + 2 not in pairs:
            continue
        params_end = pairs[index + 2]
        opening = next(
            (position for position in range(params_end + 1, len(tokens)) if tokens[position].value in {"{", ";"}), None
        )
        if opening is not None and opening in pairs and tokens[opening].value == "{":
            params = [
                tokens[first].value
                for first, _ in _arguments(tokens, pairs, index + 2, types=True)
                if tokens[first].value.isidentifier()
            ]
            helpers[tokens[index + 1].value] = (params, opening + 1, pairs[opening])
    depth = 0
    for index, token in enumerate(tokens):
        if token.value == "{" and not token.literal:
            depth += 1
        elif token.value == "}" and not token.literal:
            depth -= 1
        elif depth == 0 and token.value == "const" and index + 2 < len(tokens):
            end = next((position for position in range(index + 2, len(tokens)) if tokens[position].value == ";"), len(tokens))
            equal = next((position for position in range(index + 2, end) if tokens[position].value == "="), None)
            name = tokens[index + 1].value
            if equal is not None and name.isupper():
                kind = _foreign_value(tokens[equal + 1 : end], globals_)
                globals_[name] = "constant" if kind in COPY_KINDS else "unknown"
    functions: list[TestFunction] = []
    for index, token in enumerate(tokens):
        if rust:
            if not _is_rust_test(tokens, index):
                continue
            fn = next((position for position in range(index + 4, len(tokens)) if tokens[position].value == "fn"), None)
            if fn is None or fn + 1 >= len(tokens):
                raise _InputError(token.line, "#[test]に関数がない")
            opening = next((position for position in range(fn + 2, len(tokens)) if tokens[position].value == "{"), None)
            if opening is None or opening not in pairs:
                raise _InputError(token.line, "試験本文を解析できない")
            body, end = opening + 1, pairs[opening]
            name = tokens[fn + 1].value
            line = tokens[fn].line
        else:
            if token.value not in {"test", "it"} or (index and tokens[index - 1].value == "."):
                continue
            call = index + 1
            while call + 1 < len(tokens) and tokens[call].value == ".":
                call += 2
            if call >= len(tokens) or tokens[call].value != "(" or call not in pairs:
                continue
            span = _ts_test_body(tokens, pairs, call)
            if span is None:
                continue
            body, end = span
            name = tokens[call + 1].value.strip("\"'`")
            line = token.line
        functions.append(
            TestFunction(
                f"{name}@{line}",
                line,
                token.line,
                tokens[end].line,
                tokens[body].line,
                _foreign_checks(tokens, pairs, body, end, helpers, globals_),
            )
        )
    return functions, comments
