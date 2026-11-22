import re

import pytest

from pyfltr.comment_check.check import candidates


@pytest.mark.parametrize(
    ("suffix", "source", "expected"),
    [
        (
            ".py",
            '#!/usr/bin/env python\n# coding: utf-8\n# /// script\n# requires-python = ">=3.11"\n# ///\n'
            '"""runtime docs pyfltr: allow-comment"""\nx = "#"  # noqa\n# forbidden\n',
            "forbidden",
        ),
        (".pyi", 'def fn():\n    """forbidden"""\n', "forbidden"),
        (".py", 'class A:\n    x: int = 1\n    """forbidden"""\n', "forbidden"),
        (".py", 'def fn():\n    y = 1\n    """forbidden"""\n    return y\n', "forbidden"),
        (".py", 'import os\n"""forbidden"""\n', "forbidden"),
        (".py", 'x = "literal"\nprint("literal")\n', None),
        (".ts", "const a = /https?:\\/\\//; const b = `// literal`; // forbidden", "forbidden"),
        (".js", '// ==UserScript==\n// @name Project\n// ==/UserScript==\nlet a = "//"; /* forbidden */', "forbidden"),
        (".jsx", "const a = <div>{/* forbidden */}</div>;", "forbidden"),
        (".svelte", '<script>let a = "//"; // forbidden\n</script>', "forbidden"),
        (".html", '<div title="<!-- literal -->"><!-- forbidden --></div>', "forbidden"),
        (".css", 'a { content: "/* literal */"; } /* forbidden */', "forbidden"),
        (".rs", 'let s = r#"// literal"#; // SAFETY: invariant\n/* forbidden */', "forbidden"),
        (".cs", 'var s = @"// literal "" text"; // forbidden', "forbidden"),
        (".sh", 'echo "# literal" ${value#word}\ncat <<EOF\n# literal\nEOF\n# forbidden', "forbidden"),
        (".ps1", '$a = "# literal"\n<# forbidden #>', "forbidden"),
        (".tsx", "const a = <p>https://example.org{/* forbidden */}</p>;", "forbidden"),
        (".ts", "const a = `literal ${value /* forbidden */}`;", "forbidden"),
        (".svelte", "<p value={value /* forbidden */}>https://example.org</p>", "forbidden"),
        (".cs", 'var s = $"value: {"// literal"}"; // forbidden', "forbidden"),
        (".cs", 'var s = $$"""value: {{42 /* forbidden */}}""";', "forbidden"),
        (".sh", "echo \"$(printf 'a' # forbidden\n)\"", "forbidden"),
        (".js", "x++ / y /* forbidden */", "forbidden"),
        (".js", "function f() {} /[//]/.test(s); // forbidden", "forbidden"),
        (".bash", "cat <<\\EOF\n# literal\nEOF\n# forbidden", "forbidden"),
        (".svelte", "<p>{a < b /* forbidden */}</p>", "forbidden"),
        (".bash", "cat <<'EOF-1'\n# literal\nEOF-1\n# forbidden", "forbidden"),
        (".sh", 'cat <<-"EOF 2"\n\t# literal\n\tEOF 2\n# forbidden', "forbidden"),
        (".sh", "cat <<E'OF-'2\n# literal\nEOF-2\n# forbidden", "forbidden"),
        (".svelte", "<p>{a<b /* forbidden */}</p>", "forbidden"),
        (".svelte", "<input /><script>const s = '// literal';</script><p>{a<b /* forbidden */}</p>", "forbidden"),
        (".js", "try {} finally {} /[//]/.test(s); // forbidden", "forbidden"),
        (".js", "const f = function () {} / y /* forbidden */", "forbidden"),
        (".js", "function f() /* forbidden */ {} /[//]/.test(s);", "forbidden"),
        (".toml", "# excluded", None),
    ],
)
def test_candidates(suffix: str, source: str, expected: str | None) -> None:
    assert [comment.normalized for comment in candidates(source, suffix, [])] == ([] if expected is None else [expected])


@pytest.mark.parametrize(
    "directive",
    [
        "noqa: F401",
        "type: ignore[assignment]",
        "pyright: ignore",
        "pylint: disable=unused-import",
        "mypy: disable-error-code=assignment",
        "ruff: noqa",
        "fmt: off",
        "isort: skip",
        "isort: skip_file",
        "isort: split",
        "ruff: isort: skip_file",
        "pyright: strict",
        "pyright: basic",
        "pyright: reportMissingImports=false",
        'mypy: disallow-untyped-defs, disable-error-code="misc, union-attr"',
        "pragma: no cover",
        "shellcheck disable=SC2086",
        "shellcheck source=/dev/null",
        "shellcheck shell=bash",
        "nosemgrep: python.lang.security",
        "gitleaks:allow",
        "editorconfig-checker-disable-line",
        "test-copy-contract: 旧版との互換性のため固定する",
        "SPDX-License-Identifier: MIT",
        "Copyright 2026",
    ],
)
def test_directives(directive: str) -> None:
    assert not list(candidates(f"# {directive}\n", ".py", []))


@pytest.mark.parametrize(
    ("suffix", "source"),
    [
        (".ts", '/// <reference types="vite/client" />\n'),
        (".tsx", "/** @jsxImportSource preact */\n"),
        (".ts", "// @vitest-environment jsdom\n"),
        (".svelte", "<!-- svelte-ignore a11y_click_events_have_key_events -->\n<div></div>\n"),
        (".ps1", "#Requires -Version 7\n"),
    ],
)
def test_language_directives(suffix: str, source: str) -> None:
    assert not list(candidates(source, suffix, []))


@pytest.mark.parametrize("body", ["pyright: strictly speaking", "mypy: explain the workaround here"])
def test_directive_like_prose(body: str) -> None:
    assert [comment.normalized for comment in candidates(f"# {body}\n", ".py", [])] == [body]


def test_explicit_exceptions() -> None:
    source = (
        "# Navigation: update the schema and the migration together pyfltr: allow-comment\n"
        "# External   constraint: required by the runtime\n"
        "# Unapproved comment\n"
    )
    result = list(candidates(source, ".py", [re.compile(r"^External constraint:")]))
    assert [comment.normalized for comment in result] == ["Unapproved comment"]
