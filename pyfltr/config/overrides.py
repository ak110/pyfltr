"""環境変数の条件で`[tool.pyfltr]`の値を上書きする`overrides`と、条件の判定に使う`env-file`の処理。

`[[tool.pyfltr.overrides]]`の各要素は`when`（式）と上書きする設定キーを持つ。
`when`が真の要素を記載順に、その`pyproject.toml`の`[tool.pyfltr]`の値へ適用する。
適用はグローバル設定とのマージより前に行い、以降は通常の設定値と同じ経路（プリセット・言語カテゴリ・検証）を通す。

`when`の式は環境変数名・`not`・`and`・`or`・丸括弧だけからなり、優先順位はpytestの`-m`式と同じ
（`not` > `and` > `or`）。評価は専用の構文解析で行い、`eval`などコードを実行する手段を使わない。
"""

import collections.abc
import dataclasses
import os
import pathlib
import re
import typing

OVERRIDES_KEY = "overrides"
ENV_FILE_KEY = "env-file"

_FORBIDDEN_KEYS = (OVERRIDES_KEY, ENV_FILE_KEY, "custom-commands")
"""要素内に書けないキー。書かれた場合は警告してそのキーだけを無視する。"""

_TRUE_VALUES = frozenset({"1", "true"})
_FALSE_VALUES = frozenset({"", "0", "false"})

_TOKEN_RE = re.compile(r"\s*(?:(?P<paren>[()])|(?P<word>[A-Za-z_][A-Za-z0-9_]*)|(?P<other>\S))")

WarningEmitter = collections.abc.Callable[[str, str], None]
"""`(key, message)`を受け取り警告を発行する関数。"""


class WhenSyntaxError(ValueError):
    """`when`の式の構文が不正であることを示す。"""


@dataclasses.dataclass(frozen=True)
class _Var:
    name: str


@dataclasses.dataclass(frozen=True)
class _Not:
    operand: "_Expr"


@dataclasses.dataclass(frozen=True)
class _BinOp:
    op: typing.Literal["and", "or"]
    left: "_Expr"
    right: "_Expr"


_Expr = _Var | _Not | _BinOp


def parse_when(expression: str) -> _Expr:
    """`when`の式を構文解析する。不正な場合は`WhenSyntaxError`を送出する。"""
    tokens: list[str] = []
    pos = 0
    stripped_end = len(expression.rstrip())
    while pos < stripped_end:
        match = _TOKEN_RE.match(expression, pos)
        if match is None or match.group("other") is not None:
            raise WhenSyntaxError(f"使えない文字があります: {expression!r}")
        tokens.append(match.group("paren") or match.group("word"))
        pos = match.end()
    if not tokens:
        raise WhenSyntaxError("式が空です")
    parser = _Parser(tokens)
    result = parser.parse_or()
    if parser.index != len(tokens):
        raise WhenSyntaxError(f"式の途中に余分な語があります: {tokens[parser.index]!r}")
    return result


class _Parser:
    """`or` > `and` > `not` > 原子（変数・括弧）の順に解析する再帰下降構文解析。"""

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens
        self.index = 0

    def _peek(self) -> str | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> str:
        word = self._peek()
        if word is None:
            raise WhenSyntaxError("式が途中で終わっています")
        self.index += 1
        return word

    def parse_or(self) -> _Expr:
        left = self.parse_and()
        while self._peek() == "or":
            self.index += 1
            left = _BinOp("or", left, self.parse_and())
        return left

    def parse_and(self) -> _Expr:
        left = self.parse_not()
        while self._peek() == "and":
            self.index += 1
            left = _BinOp("and", left, self.parse_not())
        return left

    def parse_not(self) -> _Expr:
        if self._peek() == "not":
            self.index += 1
            return _Not(self.parse_not())
        return self.parse_atom()

    def parse_atom(self) -> _Expr:
        word = self._take()
        if word == "(":
            inner = self.parse_or()
            if self._take() != ")":
                raise WhenSyntaxError("閉じ括弧`)`が足りません")
            return inner
        if word in ("and", "or", "not", ")"):
            raise WhenSyntaxError(f"変数名か`(`が必要な位置に{word!r}があります")
        return _Var(word)


def evaluate_when(expr: _Expr, env: typing.Mapping[str, str], emit_unknown_value: WarningEmitter) -> bool:
    """式を評価する。

    環境変数は前後の空白を除いた値が`1`・`true`（大文字小文字を区別しない）なら真、
    未定義・空文字列・`0`・`false`なら偽とする。それ以外の値は偽と評価し、`emit_unknown_value`で警告する。
    """
    if isinstance(expr, _Var):
        raw = env.get(expr.name)
        if raw is None:
            return False
        value = raw.strip().lower()
        if value in _TRUE_VALUES:
            return True
        if value not in _FALSE_VALUES:
            emit_unknown_value(
                OVERRIDES_KEY,
                # 値は`.env`由来の秘匿値であり得るため、警告には変数名だけを示す。
                f"`overrides`の`when`が参照する環境変数`{expr.name}`の値は真偽を判定できないため、"
                "偽として扱いました（真は1・true、偽は未定義・空・0・false）",
            )
        return False
    if isinstance(expr, _Not):
        return not evaluate_when(expr.operand, env, emit_unknown_value)
    left = evaluate_when(expr.left, env, emit_unknown_value)
    if expr.op == "and":
        return left and evaluate_when(expr.right, env, emit_unknown_value)
    return left or evaluate_when(expr.right, env, emit_unknown_value)


def read_env_file(path: pathlib.Path) -> dict[str, str]:
    """`.env`形式のファイルを読み、変数名から値への辞書を返す。ファイルが無ければ空の辞書を返す。

    対応する書式は`KEY=VALUE`、行頭の`export`、単一引用符・二重引用符で囲んだ値、
    `#`で始まるコメント行と空行である。`.env`はバージョン管理外に置かれCIやclone直後には無いのが通常のため、
    不在は警告せず空として扱う。
    """
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[name] = value
    return values


def _is_extend_key(key: str) -> bool:
    """元の値と連結する追加用のキーかを返す。"""
    return key == "extend-exclude" or key.endswith(("-exclude", "-extend-args", "-extend-targets"))


def apply_overrides(
    project_data: dict[str, typing.Any],
    base_dir: pathlib.Path,
    emit_warning: WarningEmitter,
    *,
    environ: typing.Mapping[str, str] | None = None,
) -> dict[str, typing.Any]:
    """`project_data`の`overrides`を評価して適用した新しい辞書を返す。

    返す辞書から`overrides`キーは除く。`env-file`キーは通常の設定値として残す。
    追加用のキー（`extend-exclude`、`{command}-exclude`、`{command}-extend-args`、`{command}-extend-targets`）は
    元の値と真になった各要素の値を記載順に連結し、それ以外のキーは置き換える（複数要素では後の要素の値）。
    不正な要素（`when`が無い・文字列でない・構文が不正）は警告して要素全体を無視する。
    """
    normalized = {key.replace("_", "-"): value for key, value in project_data.items()}
    result = {key: value for key, value in normalized.items() if key != OVERRIDES_KEY}
    raw_overrides = normalized.get(OVERRIDES_KEY)
    if raw_overrides is None:
        return result
    if not isinstance(raw_overrides, list):
        emit_warning(
            OVERRIDES_KEY,
            "`overrides`はテーブルの配列（`[[tool.pyfltr.overrides]]`）で指定してください。無視しました",
        )
        return result

    env: dict[str, str] = {}
    env_file = normalized.get(ENV_FILE_KEY, "")
    if isinstance(env_file, str) and env_file:
        env.update(read_env_file(base_dir / env_file))
    # 同名の変数がプロセスにある場合はプロセスの値を優先する。
    env.update(os.environ if environ is None else environ)

    for position, element in enumerate(raw_overrides, start=1):
        if not isinstance(element, dict):
            emit_warning(OVERRIDES_KEY, f"`overrides`の{position}番目の要素がテーブルではないため無視しました")
            continue
        when = element.get("when")
        if not isinstance(when, str):
            emit_warning(OVERRIDES_KEY, f"`overrides`の{position}番目の要素に文字列の`when`が無いため、要素全体を無視しました")
            continue
        try:
            expr = parse_when(when)
        except WhenSyntaxError as e:
            emit_warning(
                OVERRIDES_KEY,
                f"`overrides`の{position}番目の要素の`when`の構文が不正なため、要素全体を無視しました: {e}",
            )
            continue
        if not evaluate_when(expr, env, emit_warning):
            continue
        for raw_key, value in element.items():
            key = raw_key.replace("_", "-")
            if key == "when":
                continue
            if key in _FORBIDDEN_KEYS:
                emit_warning(
                    OVERRIDES_KEY, f"`overrides`の要素には`{key}`を書けないため、このキーを無視しました（{position}番目の要素）"
                )
                continue
            base = result.get(key)
            if _is_extend_key(key) and isinstance(base, list) and isinstance(value, list):
                result[key] = [*base, *value]
            else:
                result[key] = value
    return result
