import ast
import io
import re
import tokenize
from bisect import bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
from html.parser import HTMLParser

SUFFIXES = (
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".svelte",
    ".css",
    ".html",
    ".rs",
    ".cs",
    ".sh",
    ".bash",
    ".ps1",
    ".psm1",
    ".psd1",
)
_JS = frozenset(SUFFIXES[2:8])
_SHELL = {".sh", ".bash"}
_PS = {".ps1", ".psm1", ".psd1"}
_DIRECTIVE = re.compile(
    r"^(?:noqa\b|type:\s*ignore\b|"
    r"(?:pyright|ty|mypy|pylint|ruff|fmt|isort|shellcheck|shfmt|pragma|coverage):?\s*"
    r"(?:disable|enable|noqa|ignore|off|on|skip|no cover|no branch|file-ignore)\b|"
    r"eslint(?:-(?:disable(?:-next-line|-line)?|enable|env))?\b|"
    r"(?:biome-ignore(?:-all|-start|-end)?|oxlint-(?:disable(?:-next-line|-line)?|enable)|"
    r"prettier-ignore(?:-start|-end)?|stylelint-(?:disable(?:-next-line|-line)?|enable)|"
    r"@ts-(?:ignore|expect-error|nocheck|check)|(?:istanbul|c8|v8) ignore|"
    r"NOSONAR|nosec|Copyright)\b|SAFETY:|SPDX-(?:License-Identifier|FileCopyrightText):)",
    re.IGNORECASE,
)
_ENCODING = re.compile(r"^#.*?coding[:=][ \t]*[-\w.]+")
_REGEX = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\\n])*\]|[^/\[\\\n])+/[a-z]*")
_HEREDOC = re.compile(r"<<(-?)[ \t]*")
_SVELTE_RAW_TAG = re.compile(r"<(script|style)(?=[\s>])(?:[^>\"']|\"[^\"]*\"|'[^']*')*>", re.IGNORECASE)
_RUST_CHAR = re.compile(r"'(?:\\(?:u\{[0-9a-fA-F_]+\}|x[0-9a-fA-F]{2}|.)|[^'\n])'")
_RUST_RAW = re.compile(r'(?:br|cr|r)(#*)"')
_CS_RAW = re.compile(r'(\$*)("{3,})')
_CS_INTERPOLATED = re.compile(r'(?:\$@|@\$|\$)"')
_JSX_TAG = re.compile(r"<(?:[a-zA-Z][\w.:-]*)?(?=[\s/>])")
_IDENTIFIER = re.compile(r"[\w$]+")


@dataclass(frozen=True, slots=True)
class Comment:
    line: int
    column: int
    body: str
    docstring: bool = False

    @property
    def normalized(self) -> str:
        return " ".join(self.body.split())


def python_comments(source: str) -> Iterator[Comment]:
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            line, column = token.start
            yield Comment(line, column + 1, token.string[1:].strip())
    lines = source.split("\n")
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(expr := node.body[0], ast.Expr)
            and isinstance(value := expr.value, ast.Constant)
            and isinstance(value.value, str)
        ):
            column = len(lines[expr.lineno - 1].encode()[: expr.col_offset].decode()) + 1
            yield Comment(expr.lineno, column, value.value, docstring=True)


def _quoted_end(source: str, start: int, delimiter: str, escape: str, doubled: bool = False) -> int:
    i = start
    while i < len(source):
        if escape and source.startswith(escape, i):
            i += 2
        elif doubled and source.startswith(delimiter * 2, i):
            i += 2 * len(delimiter)
        elif source.startswith(delimiter, i):
            return i + len(delimiter)
        else:
            i += 1
    return len(source)


@dataclass(frozen=True, slots=True)
class _Quote:
    delimiter: str
    opener: str
    closer: str
    escape: str = ""
    doubled: bool = False
    literal_open: str = ""

    def next_piece(self, source: str, start: int) -> tuple[int, bool]:
        i = start
        while i < len(source):
            if self.escape and source.startswith(self.escape, i):
                i += len(self.escape) + 1
            elif self.doubled and source.startswith(self.delimiter * 2, i):
                i += 2 * len(self.delimiter)
            elif self.literal_open and source.startswith(self.literal_open, i):
                i += len(self.literal_open)
            elif source.startswith(self.delimiter, i):
                return i + len(self.delimiter), False
            elif source.startswith(self.opener, i):
                return i + len(self.opener), True
            else:
                i += 1
        return i, False


def _block_end(source: str, start: int, opening: str, closing: str, nested: bool) -> int:
    depth = 1
    i = start
    while i < len(source):
        if source.startswith(closing, i):
            depth -= 1
            if depth == 0:
                return i
            i += len(closing)
        elif nested and source.startswith(opening, i):
            depth += 1
            i += len(opening)
        else:
            i += 1
    return len(source)


def _line_end(source: str, start: int) -> int:
    end = source.find("\n", start)
    return len(source) if end < 0 else end


def _heredoc_word(source: str, start: int) -> tuple[str, int] | None:
    parts: list[str] = []
    i = start
    while i < len(source) and source[i] not in " \t\n;|&()<>":
        char = source[i]
        if char in {"'", '"'}:
            end = _quoted_end(source, i + 1, char, "\\" if char == '"' else "")
            if end <= i + 1 or source[end - 1] != char:
                return None
            part = source[i + 1 : end - 1]
            parts.append(re.sub(r'\\(["\\$`])', r"\1", part) if char == '"' else part)
            i = end
        elif char == "\\" and i + 1 < len(source):
            if source[i + 1] != "\n":
                parts.append(source[i + 1])
            i += 2
        else:
            parts.append(char)
            i += 1
    return ("".join(parts), i) if parts else None


def _code_offsets(source: str, suffix: str, *, svelte: bool = False) -> Iterator[tuple[int, str]]:
    i = 0
    regex_allowed = True
    control_keyword = False
    control_parens: list[tuple[bool, bool | None]] = []
    pending_function: bool | None = None
    function_body: bool | None = None
    previous_symbol = ""
    js_blocks: list[bool] = []
    block_keyword = False
    js = suffix in _JS
    shell = suffix in _SHELL
    ps = suffix in _PS
    slash = js or suffix in {".rs", ".cs"}
    expressions: list[tuple[_Quote | str, int]] = []
    jsx = suffix in {".tsx", ".jsx"}
    jsx_roots: list[int] = [1] if svelte else []
    jsx_mode = "text" if svelte else ""
    jsx_closing = False
    heredocs: list[tuple[str, bool]] = []
    while i < len(source):
        char = source[i]
        if jsx_mode:
            if svelte and jsx_mode == "text" and source.startswith("<!--", i):
                end = _block_end(source, i + 4, "<!--", "-->", False)
                yield i, source[i + 4 : end].strip()
                i = end + 3
                continue
            if svelte and jsx_mode == "text" and (raw_tag := _SVELTE_RAW_TAG.match(source, i)):
                name = raw_tag[1].lower()
                closing = re.compile(rf"</{name}\s*>", re.IGNORECASE).search(source, raw_tag.end())
                end = closing.start() if closing else len(source)
                language = ".js" if name == "script" else ".css"
                for offset, body in _code_offsets(source[raw_tag.end() : end], language):
                    yield raw_tag.end() + offset, body
                i = closing.end() if closing else end
                continue
            if char == "{":
                expressions.append((jsx_mode, 0))
                jsx_mode = ""
                regex_allowed = True
            elif jsx_mode == "text" and char == "<" and (_JSX_TAG.match(source, i) or source.startswith("</", i)):
                jsx_mode = "tag"
                jsx_closing = source.startswith("</", i)
                if jsx_closing:
                    i += 1
            elif jsx_mode == "tag" and char in {"'", '"'}:
                i = _quoted_end(source, i + 1, char, "")
                continue
            elif jsx_mode == "tag" and char == ">":
                self_closing = source[i - 1] == "/"
                jsx_roots[-1] += -1 if jsx_closing else int(not self_closing)
                if jsx_roots[-1] or svelte:
                    jsx_mode = "text"
                else:
                    jsx_roots.pop()
                    jsx_mode = ""
                    regex_allowed = False
            i += 1
            continue
        if jsx and not svelte and regex_allowed and char == "<" and _JSX_TAG.match(source, i):
            jsx_roots.append(0)
            jsx_mode = "tag"
            jsx_closing = False
            i += 1
            continue
        if expressions:
            resume, depth = expressions[-1]
            closer = resume.closer if isinstance(resume, _Quote) else "}"
            opening = "(" if closer == ")" else "{"
            if depth == 0 and source.startswith(closer, i):
                expressions.pop()
                i += len(closer)
                if isinstance(resume, str):
                    jsx_mode = resume
                    if jsx_mode == "tag":
                        jsx_closing = False
                else:
                    i, hole = resume.next_piece(source, i)
                    if hole:
                        expressions.append((resume, 0))
                    regex_allowed = hole
                continue
            if char in {opening, closer[0]}:
                expressions[-1] = resume, depth + (1 if char == opening else -1)
        if heredocs and char == "\n":
            for marker, strip_tabs in heredocs:
                while i < len(source):
                    start = i + 1
                    i = _line_end(source, start)
                    line = source[start:i]
                    if (line.lstrip("\t") if strip_tabs else line) == marker:
                        break
            heredocs.clear()
        elif (
            shell
            and not source.startswith("<<<", i)
            and not (i and source[i - 1] == "<")
            and (heredoc := _HEREDOC.match(source, i))
            and (word := _heredoc_word(source, heredoc.end()))
        ):
            marker, i = word
            heredocs.append((marker, bool(heredoc[1])))
            continue
        elif (slash and source.startswith("//", i)) or (
            char == "#" and (ps or (shell and (i == 0 or source[i - 1] in " \t\n;|&()<>")))
        ):
            end = _line_end(source, i)
            yield i, source[i + (2 if slash else 1) : end].strip()
            i = end
            continue
        elif (ps and source.startswith("<#", i)) or ((slash or suffix == ".css") and source.startswith("/*", i)):
            end = _block_end(source, i + 2, "<#" if ps else "/*", "#>" if ps else "*/", suffix == ".rs")
            yield i, source[i + 2 : end].strip()
            i = end + 2
            continue
        elif suffix == ".rs" and (raw := _RUST_RAW.match(source, i)):
            i = _quoted_end(source, raw.end(), '"' + raw[1], "")
            continue
        elif suffix == ".cs" and (raw := _CS_RAW.match(source, i)):
            if raw[1]:
                size = len(raw[1])
                quote = _Quote(raw[2], "{" * size, "}" * size)
                i, hole = quote.next_piece(source, raw.end())
                if hole:
                    expressions.append((quote, 0))
            else:
                i = _quoted_end(source, raw.end(), raw[2], "")
            continue
        elif suffix == ".cs" and (interpolated := _CS_INTERPOLATED.match(source, i)):
            verbatim = "@" in interpolated[0]
            quote = _Quote('"', "{", "}", "" if verbatim else "\\", doubled=verbatim, literal_open="{{")
            i, hole = quote.next_piece(source, interpolated.end())
            if hole:
                expressions.append((quote, 0))
            continue
        elif suffix == ".cs" and source.startswith('@"', i):
            i = _quoted_end(source, i + 2, '"', "", doubled=True)
            continue
        elif ps and source[i : i + 2] in {"@'", '@"'}:
            delimiter = source[i + 1] + "@"
            end = source.find("\n" + delimiter, i + 2)
            i = len(source) if end < 0 else end + 3
            continue
        elif (js and char == "`") or (shell and char == '"'):
            quote = _Quote(char, "${" if js else "$(", "}" if js else ")", "\\")
            i, hole = quote.next_piece(source, i + 1)
            if hole:
                expressions.append((quote, 0))
            regex_allowed = hole
            continue
        elif char in {"'", '"'} or (shell and char == "`"):
            if suffix != ".rs" or char != "'" or _RUST_CHAR.match(source, i):
                escape = "`" if ps else "" if shell and char == "'" else "\\"
                i = _quoted_end(source, i + 1, char, escape, doubled=ps)
                regex_allowed = False
                continue
        elif js and char == "/" and regex_allowed and (literal := _REGEX.match(source, i)):
            i = literal.end()
            regex_allowed = False
            continue
        elif (
            (js or shell or ps or suffix == ".css")
            and char == ("`" if ps else "\\")
            or js
            and source.startswith(("++", "--"), i)
        ):
            i += 2
            continue
        elif js and (identifier := _IDENTIFIER.match(source, i)):
            if identifier[0] == "function":
                pending_function = previous_symbol in {"", ";", "{", "}", "export", "default"}
            block_keyword = identifier[0] in {"else", "try", "finally", "do"}
            control_keyword = identifier[0] in {"if", "while", "for", "switch", "with", "catch"}
            regex_allowed = identifier[0] in {"return", "throw", "case", "yield", "typeof", "void", "delete", "await"}
            i = identifier.end()
            previous_symbol = identifier[0]
            continue
        elif js and not char.isspace():
            if char == "(":
                control_parens.append((control_keyword, pending_function))
                pending_function = None
            if char == "{":
                block = block_keyword or (not previous_symbol and not expressions) or previous_symbol in {";", ")", "{", "}"}
                js_blocks.append(block if function_body is None else function_body)
            function_body = None
            if char == "}" and js_blocks:
                regex_allowed = js_blocks.pop()
            elif char == ")" and control_parens:
                regex_allowed, function_body = control_parens.pop()
            else:
                regex_allowed = char in "=([{,:;!&|?+-*%~^<>/"
            previous_symbol = char
            control_keyword = block_keyword = False
        i += 1


class _MarkupComments(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.line_starts = _line_starts(source)
        self.offsets: list[tuple[int, str]] = []
        self.language = ""

    def handle_comment(self, data: str) -> None:
        line, column = self.getpos()
        self.offsets.append((self.line_starts[line - 1] + column, data.strip()))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            script_type = dict(attrs).get("type") or ""
            self.language = ".css" if tag == "style" else ".js" if "json" not in script_type else ""

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.language = ""

    def handle_data(self, data: str) -> None:
        if self.language:
            self._append_code(data, self.language)

    def _append_code(self, data: str, suffix: str) -> None:
        line, column = self.getpos()
        start = self.line_starts[line - 1] + column
        self.offsets.extend((start + offset, body) for offset, body in _code_offsets(data, suffix))


def _line_starts(source: str) -> list[int]:
    return [0, *(match.end() for match in re.finditer("\n", source))]


def extract(source: str, suffix: str) -> list[Comment]:
    if suffix in {".py", ".pyi"}:
        return sorted(python_comments(source), key=lambda comment: (comment.line, comment.column))
    if suffix not in SUFFIXES:
        return []
    offsets: Iterator[tuple[int, str]]
    if suffix == ".html":
        parser = _MarkupComments(source)
        parser.feed(source)
        parser.close()
        offsets = iter(parser.offsets)
    elif suffix == ".svelte":
        offsets = _code_offsets(source, ".tsx", svelte=True)
    else:
        offsets = _code_offsets(source, suffix)
    lines = _line_starts(source)
    result = []
    for offset, body in offsets:
        line = bisect_right(lines, offset)
        result.append(Comment(line, offset - lines[line - 1] + 1, body))
    return result


def candidates(source: str, suffix: str, allow: list[re.Pattern[str]]) -> Iterator[Comment]:
    comments = extract(source, suffix)
    lines = source.split("\n")
    markers = {
        ".py": ("/// script", "///"),
        ".pyi": ("/// script", "///"),
        **dict.fromkeys(_JS, ("==UserScript==", "==/UserScript==")),
    }
    opening, closing = markers.get(suffix, ("", ""))
    protected: set[int] = set()
    start: int | None = None
    for i, comment in enumerate(comments):
        if comment.docstring or not closing:
            continue
        if start is not None and (
            comment.line != comments[i - 1].line + 1
            or lines[comment.line - 1][: comment.column - 1].strip()
            or not lines[comment.line - 1][comment.column - 1 :].startswith("#" if suffix in {".py", ".pyi"} else "//")
        ):
            start = None
        if (
            comment.body == opening
            and not lines[comment.line - 1][: comment.column - 1].strip()
            and lines[comment.line - 1][comment.column - 1 :].startswith("#" if suffix in {".py", ".pyi"} else "//")
        ):
            start = i
        elif start is not None and comment.body == closing:
            protected.update(range(start, i + 1))
            start = None
    for i, comment in enumerate(comments):
        line = lines[comment.line - 1]
        if i in protected or (
            not comment.docstring
            and (
                (comment.line == 1 and comment.column == 1 and line.startswith("#!/"))
                or (suffix in {".py", ".pyi"} and comment.line <= 2 and _ENCODING.match(line.lstrip()))
                or _DIRECTIVE.match(comment.body)
            )
        ):
            continue
        if "pyfltr: allow-comment" not in comment.body and not any(pattern.search(comment.normalized) for pattern in allow):
            yield comment
