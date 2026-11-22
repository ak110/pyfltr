import re
import typing

import pyfltr.tools

RuleUrlBuilder = typing.Callable[[str, str | None], str | None]
"""rule / category を受け取り URL を返す関数シグネチャ。"""


def build_ruff_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ（ruffはカテゴリーを使わない）
    return f"https://docs.astral.sh/ruff/rules/{rule}/"


def build_pylint_url(rule: str, category: str | None) -> str | None:
    if category is None:
        return None
    return f"https://pylint.readthedocs.io/en/stable/user_guide/messages/{category}/{rule}.html"


def build_pyright_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://microsoft.github.io/pyright/#/configuration?id={rule}"


def build_mypy_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://mypy.readthedocs.io/en/stable/_refs.html#code-{rule}"


def build_shellcheck_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://www.shellcheck.net/wiki/{rule}"


def build_eslint_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    # ESLintのプラグインルールは`plugin/rule`形式で、
    if "/" in rule:
        return None
    return f"https://eslint.org/docs/latest/rules/{rule}"


def build_markdownlint_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://github.com/DavidAnson/markdownlint/blob/main/doc/{rule}.md"


def build_biome_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    # ルール名はキャメルケースで、公式ドキュメントのslugはケバブケースとなる。
    group, _, name = rule.rpartition("/")
    if not name:
        return None
    slug = re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()
    if group.startswith("lint/"):
        return f"https://biomejs.dev/linter/rules/{slug}/"
    if group == "assist/source":
        return f"https://biomejs.dev/assist/actions/{slug}/"
    return None


def build_rule_url(
    command: str,
    rule: str | None,
    *,
    existing_url: str | None = None,
    category: str | None = None,
) -> str | None:
    if existing_url:
        return existing_url
    if not rule:
        return None
    builder = info.rule_url_builder if (info := pyfltr.tools.BUILTIN_COMMANDS.get(command)) else None
    if builder is None:
        return None
    return builder(rule, category)
