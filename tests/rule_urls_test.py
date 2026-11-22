import pyfltr.rule_urls


def test_build_rule_url_ruff_existing_priority() -> None:
    url = pyfltr.rule_urls.build_rule_url("ruff-check", "F401", existing_url="https://example.com/x")
    assert url == "https://example.com/x"


def test_build_rule_url_ruff_fallback() -> None:
    url = pyfltr.rule_urls.build_rule_url("ruff-check", "F401")
    assert url == "https://docs.astral.sh/ruff/rules/F401/"


def test_build_rule_url_pylint_requires_category() -> None:
    assert pyfltr.rule_urls.build_rule_url("pylint", "missing-module-docstring") is None


def test_build_rule_url_pylint_with_category() -> None:
    url = pyfltr.rule_urls.build_rule_url("pylint", "missing-module-docstring", category="convention")
    assert url == "https://pylint.readthedocs.io/en/stable/user_guide/messages/convention/missing-module-docstring.html"


def test_build_rule_url_pyright() -> None:
    url = pyfltr.rule_urls.build_rule_url("pyright", "reportAssignmentType")
    assert url == "https://microsoft.github.io/pyright/#/configuration?id=reportAssignmentType"


def test_build_rule_url_mypy() -> None:
    url = pyfltr.rule_urls.build_rule_url("mypy", "name-defined")
    assert url == "https://mypy.readthedocs.io/en/stable/_refs.html#code-name-defined"


def test_build_rule_url_shellcheck() -> None:
    url = pyfltr.rule_urls.build_rule_url("shellcheck", "SC2086")
    assert url == "https://www.shellcheck.net/wiki/SC2086"


def test_build_rule_url_eslint_core() -> None:
    url = pyfltr.rule_urls.build_rule_url("eslint", "no-unused-vars")
    assert url == "https://eslint.org/docs/latest/rules/no-unused-vars"


def test_build_rule_url_eslint_plugin_unsupported() -> None:
    assert pyfltr.rule_urls.build_rule_url("eslint", "@typescript-eslint/no-explicit-any") is None


def test_build_rule_url_markdownlint() -> None:
    url = pyfltr.rule_urls.build_rule_url("markdownlint", "MD001")
    assert url == "https://github.com/DavidAnson/markdownlint/blob/main/doc/MD001.md"


def test_build_rule_url_biome_lint() -> None:
    url = pyfltr.rule_urls.build_rule_url("biome", "lint/complexity/useLiteralKeys")
    assert url == "https://biomejs.dev/linter/rules/use-literal-keys/"


def test_build_rule_url_biome_assist() -> None:
    url = pyfltr.rule_urls.build_rule_url("biome", "assist/source/organizeImports")
    assert url == "https://biomejs.dev/assist/actions/organize-imports/"


def test_build_rule_url_biome_without_category_hierarchy() -> None:
    assert pyfltr.rule_urls.build_rule_url("biome", "format") is None
    assert pyfltr.rule_urls.build_rule_url("biome", "parse") is None


def test_build_rule_url_ty_unsupported() -> None:
    assert pyfltr.rule_urls.build_rule_url("ty", "invalid-argument-type") is None
    assert pyfltr.rule_urls.build_rule_url("ty", "revealed-type") is None


def test_build_rule_url_actionlint_unsupported() -> None:
    assert pyfltr.rule_urls.build_rule_url("actionlint", "expression") is None


def test_build_rule_url_textlint_unsupported() -> None:
    assert pyfltr.rule_urls.build_rule_url("textlint", "ja-technical-writing/ja-no-mixed-period") is None


def test_build_rule_url_empty_rule() -> None:
    assert pyfltr.rule_urls.build_rule_url("ruff-check", None) is None
    assert pyfltr.rule_urls.build_rule_url("ruff-check", "") is None


def test_build_rule_url_unknown_command() -> None:
    assert pyfltr.rule_urls.build_rule_url("unknown-tool", "X123") is None
