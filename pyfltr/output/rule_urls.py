"""ツール別のルールドキュメント URL を組み立てる。

各カスタムパーサーから呼び出され、`ErrorLocation.rule_url`に格納するURLを返す。
ruffはJSON出力に含まれる`url`フィールドを最優先で採用し、なければテンプレートで
補完する。pylintは公式ドキュメントが`.../messages/<category>/<symbol>.html`の
symbol基準のため、`category`引数で経路を分岐する。textlintはプラグインごとに
URL体系が揃わないため未サポート。biomeは診断カテゴリーが`lint/<group>/<rule>`と
`assist/source/<action>`の2体系を持ち、それぞれ`/linter/rules/`と
`/assist/actions/`へ分岐する。`format`・`parse`のように階層を持たないカテゴリーは
ドキュメントページを持たないためURLを返さない。
tyは診断コードのうちルールに当たるものだけがドキュメントページのアンカーを持ち、
`revealed-type`・`invalid-syntax`のようにルールでない診断コードは持たない。
両者を識別子の字面から区別できず、`ty explain rule <識別子>`の実行を要するため未サポート。
actionlintは公式ドキュメントの見出しとrule識別子が多対多で対応し機械的に導けないため未サポート。
"""

import re
import typing

_RuleUrlBuilder = typing.Callable[[str, str | None], str | None]
"""rule / category を受け取り URL を返す関数シグネチャ。"""


def _build_ruff_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ（ruffはカテゴリーを使わない）
    return f"https://docs.astral.sh/ruff/rules/{rule}/"


def _build_pylint_url(rule: str, category: str | None) -> str | None:
    if category is None:
        return None
    return f"https://pylint.readthedocs.io/en/stable/user_guide/messages/{category}/{rule}.html"


def _build_pyright_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://microsoft.github.io/pyright/#/configuration?id={rule}"


def _build_mypy_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://mypy.readthedocs.io/en/stable/_refs.html#code-{rule}"


def _build_shellcheck_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://www.shellcheck.net/wiki/{rule}"


def _build_eslint_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    # ESLintのプラグインルールは`plugin/rule`形式で、
    # 中央ドキュメントでは個別に辿れないため本体ルールのみURLを返す。
    if "/" in rule:
        return None
    return f"https://eslint.org/docs/latest/rules/{rule}"


def _build_markdownlint_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    return f"https://github.com/DavidAnson/markdownlint/blob/main/doc/{rule}.md"


def _build_biome_url(rule: str, category: str | None) -> str | None:
    del category  # シグネチャ互換のため受け取るのみ
    # biomeの診断カテゴリーは`lint/<group>/<rule>`・`assist/source/<action>`・
    # `format`・`parse`の4形態を取る。ドキュメントページを持つのは前2者のみ。
    # ルール名はキャメルケースで、公式ドキュメントのslugはケバブケースとなる。
    # 大文字の直前へハイフンを挿入する変換は、ルール名が数字と連続大文字を含まない限り
    # slugと一致する。biomeのルール命名がこの規則を保つ限り版を問わず成立するため、
    # 特定の版のルール件数を前提としない。pyfltrはbiomeの版を固定しない。
    # biomejs.devは版別パスを持たず、旧版のnurseryルールが改名・削除された場合は404となりうる。
    # 版別対応表は実行版の判定と継続更新を要するため持たず、現行ドキュメントの規則でURLを生成する。
    group, _, name = rule.rpartition("/")
    if not name:
        return None
    slug = re.sub(r"(?<!^)(?=[A-Z])", "-", name).lower()
    if group.startswith("lint/"):
        return f"https://biomejs.dev/linter/rules/{slug}/"
    if group == "assist/source":
        return f"https://biomejs.dev/assist/actions/{slug}/"
    return None


_BUILDERS: dict[str, _RuleUrlBuilder] = {
    "ruff-check": _build_ruff_url,
    "pylint": _build_pylint_url,
    "pyright": _build_pyright_url,
    "mypy": _build_mypy_url,
    "shellcheck": _build_shellcheck_url,
    "eslint": _build_eslint_url,
    "markdownlint": _build_markdownlint_url,
    "biome": _build_biome_url,
}


def build_rule_url(
    command: str,
    rule: str | None,
    *,
    existing_url: str | None = None,
    category: str | None = None,
) -> str | None:
    """Rule から URL を生成する。

    `existing_url`が非Noneならそれを最優先で採用する（ruff JSONの`url`
    フィールドを保持するため）。`command`がテンプレート未登録、または`rule`
    が空のときは`None`を返す。
    """
    if existing_url:
        return existing_url
    if not rule:
        return None
    builder = _BUILDERS.get(command)
    if builder is None:
        return None
    return builder(rule, category)
