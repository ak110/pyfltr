"""rule_urlsのテストコード。"""

import pyfltr.rule_urls


def test_build_rule_url_ruff_existing_priority() -> None:
    """ruff は existing_url を最優先で採用する。"""
    url = pyfltr.rule_urls.build_rule_url("ruff-check", "F401", existing_url="https://example.com/x")
    assert url == "https://example.com/x"


def test_build_rule_url_ruff_fallback() -> None:
    """ruff は existing_url が無ければテンプレートで補完する。"""
    url = pyfltr.rule_urls.build_rule_url("ruff-check", "F401")
    assert url == "https://docs.astral.sh/ruff/rules/F401/"


def test_build_rule_url_pylint_requires_category() -> None:
    """pylint は category が無ければ None を返す。"""
    assert pyfltr.rule_urls.build_rule_url("pylint", "missing-module-docstring") is None


def test_build_rule_url_pylint_with_category() -> None:
    """pylint は category 付きで URL を生成する。"""
    url = pyfltr.rule_urls.build_rule_url("pylint", "missing-module-docstring", category="convention")
    assert url == "https://pylint.readthedocs.io/en/stable/user_guide/messages/convention/missing-module-docstring.html"


def test_build_rule_url_pyright() -> None:
    """pyright の URL 生成。"""
    url = pyfltr.rule_urls.build_rule_url("pyright", "reportAssignmentType")
    assert url == "https://microsoft.github.io/pyright/#/configuration?id=reportAssignmentType"


def test_build_rule_url_mypy() -> None:
    """mypy の URL 生成。"""
    url = pyfltr.rule_urls.build_rule_url("mypy", "name-defined")
    assert url == "https://mypy.readthedocs.io/en/stable/_refs.html#code-name-defined"


def test_build_rule_url_shellcheck() -> None:
    """shellcheck の URL 生成。"""
    url = pyfltr.rule_urls.build_rule_url("shellcheck", "SC2086")
    assert url == "https://www.shellcheck.net/wiki/SC2086"


def test_build_rule_url_eslint_core() -> None:
    """eslint 本体ルールの URL 生成。"""
    url = pyfltr.rule_urls.build_rule_url("eslint", "no-unused-vars")
    assert url == "https://eslint.org/docs/latest/rules/no-unused-vars"


def test_build_rule_url_eslint_plugin_unsupported() -> None:
    """プラグインルール (plugin/rule 形式) は None を返す。"""
    assert pyfltr.rule_urls.build_rule_url("eslint", "@typescript-eslint/no-explicit-any") is None


def test_build_rule_url_markdownlint() -> None:
    """markdownlint の URL 生成。"""
    url = pyfltr.rule_urls.build_rule_url("markdownlint", "MD001")
    assert url == "https://github.com/DavidAnson/markdownlint/blob/main/doc/MD001.md"


def test_build_rule_url_textlint_unsupported() -> None:
    """textlint は未サポートで常に None を返す。"""
    assert pyfltr.rule_urls.build_rule_url("textlint", "ja-technical-writing/ja-no-mixed-period") is None


def test_build_rule_url_empty_rule() -> None:
    """空文字・None の rule は None を返す。"""
    assert pyfltr.rule_urls.build_rule_url("ruff-check", None) is None
    assert pyfltr.rule_urls.build_rule_url("ruff-check", "") is None


def test_build_rule_url_unknown_command() -> None:
    """未登録のコマンド名は None を返す。"""
    assert pyfltr.rule_urls.build_rule_url("unknown-tool", "X123") is None
