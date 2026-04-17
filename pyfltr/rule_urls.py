"""ツール別のルールドキュメント URL を組み立てる。

各カスタムパーサーから呼び出され、``ErrorLocation.rule_url`` に格納する URL を返す。
ruff は JSON 出力に含まれる ``url`` フィールドを最優先で採用し、なければテンプレートで
補完する。pylint は公式ドキュメントが ``.../messages/<category>/<symbol>.html`` の
symbol 基準のため、``category`` 引数で経路を分岐する。textlint はプラグインごとに
URL 体系が揃わないため未サポート。
"""

import typing

_RuleUrlBuilder = typing.Callable[[str, str | None], str | None]
"""rule / category を受け取り URL を返す関数シグネチャ。"""


def _build_ruff_url(rule: str, category: str | None) -> str | None:
    del category  # noqa  # ruff はカテゴリーを使わない
    return f"https://docs.astral.sh/ruff/rules/{rule}/"


def _build_pylint_url(rule: str, category: str | None) -> str | None:
    if category is None:
        return None
    return f"https://pylint.readthedocs.io/en/stable/user_guide/messages/{category}/{rule}.html"


def _build_pyright_url(rule: str, category: str | None) -> str | None:
    del category  # noqa
    return f"https://microsoft.github.io/pyright/#/configuration?id={rule}"


def _build_mypy_url(rule: str, category: str | None) -> str | None:
    del category  # noqa
    return f"https://mypy.readthedocs.io/en/stable/_refs.html#code-{rule}"


def _build_shellcheck_url(rule: str, category: str | None) -> str | None:
    del category  # noqa
    return f"https://www.shellcheck.net/wiki/{rule}"


def _build_eslint_url(rule: str, category: str | None) -> str | None:
    del category  # noqa
    # ESLint のプラグインルールは `plugin/rule` 形式で、
    # 中央ドキュメントでは個別に辿れないため本体ルールのみ URL を返す。
    if "/" in rule:
        return None
    return f"https://eslint.org/docs/latest/rules/{rule}"


def _build_markdownlint_url(rule: str, category: str | None) -> str | None:
    del category  # noqa
    return f"https://github.com/DavidAnson/markdownlint/blob/main/doc/{rule}.md"


_BUILDERS: dict[str, _RuleUrlBuilder] = {
    "ruff-check": _build_ruff_url,
    "pylint": _build_pylint_url,
    "pyright": _build_pyright_url,
    "mypy": _build_mypy_url,
    "shellcheck": _build_shellcheck_url,
    "eslint": _build_eslint_url,
    "markdownlint": _build_markdownlint_url,
}


def build_rule_url(
    command: str,
    rule: str | None,
    *,
    existing_url: str | None = None,
    category: str | None = None,
) -> str | None:
    """Rule から URL を生成する。

    ``existing_url`` が非 None ならそれを最優先で採用する (ruff JSON の ``url``
    フィールドを保持するため)。``command`` がテンプレート未登録、または ``rule``
    が空のときは ``None`` を返す。
    """
    if existing_url:
        return existing_url
    if not rule:
        return None
    builder = _BUILDERS.get(command)
    if builder is None:
        return None
    return builder(rule, category)
