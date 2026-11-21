"""PythonのASTから比較の出自を追跡する。"""

from __future__ import annotations

import ast
import functools
import io
import pathlib
import tokenize

from pyfltr.test_copies.model import COPY_KINDS, TestFunction, combine

_HELPER_READ_ERRORS = (OSError, UnicodeError, SyntaxError)

_TRANSPARENT_METHODS = {
    "strip",
    "lstrip",
    "rstrip",
    "split",
    "splitlines",
    "lower",
    "upper",
    "decode",
    "keys",
    "values",
    "items",
    "resolve",
    "absolute",
    "joinpath",
    "with_name",
    "replace",
    "removeprefix",
    "removesuffix",
    "startswith",
    "endswith",
    "find",
    "count",
    "issubset",
    "issuperset",
    "intersection",
    "union",
    "difference",
    "join",
    "get",
}


@functools.lru_cache(maxsize=16384)
def _name(node: ast.AST) -> str:
    return ast.unparse(node)


class PythonAnalysis:
    """モジュール内の名前と静的に解決できる補助関数を追跡する。"""

    def __init__(
        self,
        tree: ast.Module,
        path: pathlib.Path | None = None,
        root: pathlib.Path | None = None,
        loaded: dict[pathlib.Path, PythonAnalysis] | None = None,
    ) -> None:
        self.path, self.root = path, root
        self.tree = tree
        self.loaded = loaded if loaded is not None else {}
        self.imports: dict[str, tuple[str, int]] = {}
        self.import_cache: dict[str, tuple[PythonAnalysis, str] | None] = {}
        if path is not None:
            self.loaded[path] = self
        self.helpers = {node.name: node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.globals: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                    self.globals[name] = "constant" if isinstance(node, ast.ImportFrom) and alias.name.isupper() else "module"
                    module = (
                        (f"{node.module}.{alias.name}" if node.module else alias.name)
                        if isinstance(node, ast.ImportFrom)
                        else (alias.name if alias.asname else name)
                    )
                    self.imports[name] = (module, node.level if isinstance(node, ast.ImportFrom) else 0)
            elif isinstance(node, ast.ClassDef):
                self.globals[node.name] = "module"
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                self.assign(node, self.globals)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and target.id.isupper() and self.globals.get(target.id) in COPY_KINDS:
                        self.globals[target.id] = "constant"

    def imported_helper(self, name: str) -> tuple[PythonAnalysis, str] | None:
        """import先の関数の解析結果を返す。"""
        if name not in self.import_cache:
            self.import_cache[name] = self._imported_helper(name)
        return self.import_cache[name]

    def _imported_helper(self, name: str) -> tuple[PythonAnalysis, str] | None:
        if self.path is None or self.root is None or name.split(".")[0] not in self.imports:
            return None
        alias, *tail = name.split(".")
        module, level = self.imports[alias]
        parts = (module + ("." + ".".join(tail) if tail else "")).split(".")
        if len(parts) < 2:
            return None
        if level > len(self.path.parents):
            return None
        bases = [self.path.parents[level - 1]] if level else list(self.path.parents)
        for base in bases:
            if not base.is_relative_to(self.root):
                continue
            module_path = base.joinpath(*parts[:-1])
            for candidate in (module_path.with_suffix(".py"), module_path / "__init__.py"):
                if not candidate.is_file() or not candidate.resolve().is_relative_to(self.root):
                    continue
                try:
                    if candidate not in self.loaded:
                        PythonAnalysis(ast.parse(candidate.read_text(encoding="utf-8")), candidate, self.root, self.loaded)
                    analysis = self.loaded[candidate]
                    if parts[-1] in analysis.helpers:
                        return analysis, parts[-1]
                except _HELPER_READ_ERRORS:
                    # 補助の静的な解決に失敗した場合は出自不明のままに保つ。
                    return None
        return None

    def value(self, node: ast.AST | None, env: dict[str, str], stack: tuple[str, ...] = ()) -> str:
        """式の値の出自を分類する。"""
        if node is None:
            return "unknown"
        if isinstance(node, ast.Constant):
            return "literal"
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return "literal"
            return env.get(node.id, "unknown")
        if isinstance(node, ast.Attribute):
            if _name(node) in env:
                return env[_name(node)]
            if node.attr.isupper():
                return "constant" if self.value(node.value, env, stack) == "module" else "unknown"
            if node.attr in {"stdout", "stderr"}:
                return env.get(_name(node), "unknown")
            return self.value(node.value, env, stack)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return combine([self.value(item, env, stack) for item in node.elts]) if node.elts else "literal"
        if isinstance(node, ast.Dict):
            return (
                combine([self.value(item, env, stack) for item in [*node.keys, *node.values] if item])
                if node.values
                else "literal"
            )
        if isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            local = dict(env)
            for generator in node.generators:
                self.assign(ast.Assign(targets=[generator.target], value=generator.iter), local, stack)
            return self.value(node.elt, local, stack)
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and node.slice.value == "additionalContext":
                return "text"
            return self.value(node.value, env, stack)
        if isinstance(node, ast.JoinedStr):
            return combine(
                [self.value(item.value if isinstance(item, ast.FormattedValue) else item, env, stack) for item in node.values]
            )
        if isinstance(node, ast.Await):
            return self.value(node.value, env, stack)
        if isinstance(node, ast.Call):
            name = _name(node.func)
            leaf = name.rsplit(".", 1)[-1]
            if (
                leaf == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "additionalContext"
            ):
                return "text"
            if leaf in {"read_text", "read_bytes"} and isinstance(node.func, ast.Attribute):
                return "text" if self.value(node.func.value, env, stack) in COPY_KINDS else "unknown"
            if leaf in {"getsource", "getdoc", "format_help", "format_usage"}:
                return "text"
            if leaf == "readouterr" and env.get("__help_output__") == "text":
                return "text"
            if leaf in {"run", "check_output", "invoke"}:
                help_call = any(isinstance(item, ast.Constant) and item.value in {"--help", "-h"} for item in ast.walk(node))
                return "text" if help_call else "behavior"
            if leaf in {"Path", "PurePath", "str", "set", "frozenset", "list", "tuple", "dict", "sorted", "len", "all", "any"}:
                return combine([self.value(item, env, stack) for item in node.args])
            if isinstance(node.func, ast.Attribute) and leaf in _TRANSPARENT_METHODS:
                return combine(
                    [self.value(node.func.value, env, stack), *[self.value(argument, env, stack) for argument in node.args]]
                )
            if name in self.helpers and name not in stack:
                helper = self.helpers[name]
                local = dict(self.globals)
                local.update(
                    {arg.arg: self.value(value, env, stack) for arg, value in zip(helper.args.args, node.args, strict=False)}
                )
                returns = [item.value for item in ast.walk(helper) if isinstance(item, ast.Return)]
                for item in helper.body:
                    if isinstance(item, (ast.Assign, ast.AnnAssign)):
                        self.assign(item, local, (*stack, name))
                result = combine([self.value(item, local, (*stack, name)) for item in returns])
                # 補助の定数戻り値は計算結果なので、静的定数自体の比較として扱わない。
                return result if result == "text" else "unknown"
            imported = self.imported_helper(name)
            if imported is not None and name not in stack:
                analysis, helper_name = imported
                arguments = {
                    f"__copy_arg_{index}": self.value(argument, env, stack) for index, argument in enumerate(node.args)
                }
                call = ast.Call(func=ast.Name(id=helper_name), args=[ast.Name(id=key) for key in arguments], keywords=[])
                return analysis.value(call, arguments, (*stack, name + ":import"))
            return "unknown"
        if isinstance(node, (ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare)):
            return combine(
                [self.value(child, env, stack) for child in ast.iter_child_nodes(node) if isinstance(child, ast.expr)]
            )
        return "unknown"

    def assign(self, node: ast.Assign | ast.AnnAssign, env: dict[str, str], stack: tuple[str, ...] = ()) -> None:
        """代入によって変わる名前の出自を記録する。"""
        kind = self.value(node.value, env, stack)
        targets: list[ast.expr] = list(reversed(node.targets)) if isinstance(node, ast.Assign) else [node.target]
        while targets:
            item = targets.pop()
            if isinstance(item, (ast.Tuple, ast.List)):
                targets.extend(reversed(item.elts))
                continue
            if isinstance(item, ast.Starred):
                targets.append(item.value)
                continue
            # 部分更新は状態全体の固定出自を無効にし、添字の変数へ右辺を伝播させない。
            if isinstance(item, (ast.Subscript, ast.Attribute)):
                owner = item.value
                while isinstance(owner, (ast.Subscript, ast.Attribute)):
                    owner = owner.value
                if isinstance(owner, ast.Name):
                    env.update({owner.id: "unknown", "__fragment:" + owner.id: "no", "__source:" + owner.id: "no"})
            elif isinstance(item, ast.Name):
                env[item.id] = kind
                env["__fragment:" + item.id] = "yes" if self.fixed_text(node.value, env) else "no"
                env["__source:" + item.id] = (
                    "yes"
                    if kind in COPY_KINDS
                    and any(
                        isinstance(child, ast.Constant)
                        and isinstance(child.value, str)
                        and child.value.endswith((".py", ".md", ".ts", ".tsx", ".rs"))
                        for child in ast.walk(node.value or ast.Constant(value=None))
                    )
                    else "no"
                )
                if kind == "text":
                    env[f"{item.id}.stdout"] = "text"
                    env[f"{item.id}.stderr"] = "text"

    def fixed_text(self, node: ast.AST | None, env: dict[str, str]) -> bool:
        """式が比較に使う固定文言を表すか判定する。"""
        if isinstance(node, ast.Constant):
            return isinstance(node.value, (str, bytes))
        if isinstance(node, ast.Name):
            return env.get("__fragment:" + node.id) == "yes"
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return bool(node.elts) and all(self.fixed_text(item, env) for item in node.elts)
        if isinstance(node, ast.JoinedStr):
            return self.value(node, env) in COPY_KINDS
        if isinstance(node, ast.BinOp):
            return self.fixed_text(node.left, env) and self.fixed_text(node.right, env)
        if isinstance(node, ast.IfExp):
            return self.fixed_text(node.body, env) and self.fixed_text(node.orelse, env)
        if isinstance(node, ast.Call) and _name(node.func).rsplit(".", 1)[-1] in {"str", "quote", "escape", "replace"}:
            receiver_fixed = (
                not isinstance(node.func, ast.Attribute)
                or node.func.attr != "replace"
                or self.value(node.func.value, env) in COPY_KINDS
            )
            return receiver_fixed and all(self.value(argument, env) in COPY_KINDS for argument in node.args)
        return False

    def text_shape(self, node: ast.AST, env: dict[str, str]) -> str | None:
        """文言の包含・先頭・末尾などの比較を分類する。"""
        if isinstance(node, ast.BoolOp):
            kinds = [self.text_shape(value, env) for value in node.values]
            return "text-only" if all(kind == "text-only" for kind in kinds) else None
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return self.text_shape(node.operand, env)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            right = node.comparators[0]
            if isinstance(node.ops[0], (ast.In, ast.NotIn)) and self.fixed_text(node.left, env):
                target = right
            elif isinstance(node.left, ast.Call) and isinstance(right, ast.Constant):
                if _name(node.left.func) == "len" and node.left.args:
                    return "text-support" if env.get("__text-target:" + _name(node.left.args[0])) == "yes" else None
                return self.text_shape(node.left, env)
            else:
                return None
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            leaf = node.func.attr
            if leaf in {"startswith", "endswith", "count"} and node.args and self.fixed_text(node.args[0], env):
                target = node.func.value
            elif leaf in {"search", "match", "fullmatch"} and len(node.args) >= 2 and self.fixed_text(node.args[0], env):
                target = node.args[1]
            elif leaf in {"is_file", "exists"} and env.get("__source:" + _name(node.func.value)) == "yes":
                return "text-support"
            else:
                return None
        else:
            return None
        if (
            self.value(target, env) != "text"
            or isinstance(target, ast.Subscript)
            or any(isinstance(child, ast.Call) and self.value(child, env) == "unknown" for child in ast.walk(target))
        ):
            return None
        env["__text-target:" + _name(target)] = "yes"
        return "text-only"

    def check(self, node: ast.AST, env: dict[str, str]) -> str:
        """assertの条件式を分類する。"""
        # 起動成功だけでは、同居する文言検査の独立契約を保証しない。
        if (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.left, ast.Attribute)
            and node.left.attr == "returncode"
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == 0
        ):
            return "startup"
        if (
            isinstance(node, ast.Compare)
            and self.value(node.left, env) == "startup"
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == 0
        ):
            return "startup"
        kind = self.value(node, env)
        shape = self.text_shape(node, env)
        if shape is not None and kind not in {"text", "constant", "behavior"}:
            return shape
        return (
            "text-only"
            if kind == "text"
            else "constant-only"
            if kind == "constant"
            else "independent"
            if kind == "behavior"
            else "unknown"
        )

    def statements(self, statements: list[ast.stmt], env: dict[str, str], stack: tuple[str, ...] = ()) -> list[str]:
        """文をたどり、出現した比較の分類を返す。"""
        checks: list[str] = []
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                self.assign(node, env, stack)
            elif isinstance(node, ast.Assert):
                checks.append(self.check(node.test, env))
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                local = dict(env)
                self.assign(ast.Assign(targets=[node.target], value=node.iter), local, stack)
                checks.extend(self.statements(node.body, local, stack))
                checks.extend(self.statements(node.orelse, dict(env), stack))
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                if any("raises" in _name(item.context_expr) for item in node.items):
                    help_exit = any(
                        isinstance(child, ast.Name) and child.id == "SystemExit"
                        for item in node.items
                        for child in ast.walk(item.context_expr)
                    ) and any(
                        isinstance(child, ast.Constant) and child.value == "--help"
                        for statement in node.body
                        for child in ast.walk(statement)
                    )
                    checks.append("startup" if help_exit else "independent")
                    if help_exit:
                        env["__help_output__"] = "text"
                        for item in node.items:
                            if isinstance(item.optional_vars, ast.Name):
                                env[item.optional_vars.id + ".value.code"] = "startup"
                checks.extend(self.statements(node.body, dict(env), stack))
            elif isinstance(node, (ast.If, ast.While)):
                checks.extend(self.statements(node.body, dict(env), stack))
                checks.extend(self.statements(node.orelse, dict(env), stack))
            elif isinstance(node, ast.Match):
                for case in node.cases:
                    checks.extend(self.statements(case.body, dict(env), stack))
            elif isinstance(node, (ast.Try, ast.TryStar)):
                checks.extend(self.statements(node.body + node.orelse + node.finalbody, dict(env), stack))
                for handler in node.handlers:
                    checks.extend(self.statements(handler.body, dict(env), stack))
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                name = _name(call.func)
                if isinstance(call.func, ast.Attribute) and call.func.attr in {
                    "append",
                    "extend",
                    "insert",
                    "update",
                    "add",
                    "discard",
                    "remove",
                    "pop",
                    "clear",
                    "setdefault",
                }:
                    env[_name(call.func.value)] = "behavior"
                if name not in self.helpers and not name.rsplit(".", 1)[-1].startswith(("assert", "check", "expect")):
                    for argument in [*call.args, *[keyword.value for keyword in call.keywords]]:
                        for child in ast.walk(argument):
                            if isinstance(child, ast.Name) and env.get(child.id) in {"literal", "constant"}:
                                env[child.id] = "unknown"
                if name in self.helpers and name not in stack:
                    helper = self.helpers[name]
                    local = dict(self.globals)
                    local.update(
                        {arg.arg: self.value(value, env) for arg, value in zip(helper.args.args, call.args, strict=False)}
                    )
                    for arg, value in zip(helper.args.args, call.args, strict=False):
                        local["__fragment:" + arg.arg] = "yes" if self.fixed_text(value, env) else "no"
                    mutating_callbacks = {
                        item.name
                        for item in helper.body
                        if isinstance(item, ast.FunctionDef)
                        and any(
                            isinstance(child, ast.Assign)
                            and any(isinstance(target, (ast.Subscript, ast.Attribute)) for target in child.targets)
                            for child in ast.walk(item)
                        )
                    }
                    # 更新callbackの成功確認だけを持つhelperは、関数の結果検査でなく状態準備として扱う。
                    if mutating_callbacks and all(
                        isinstance(item.test, ast.Call)
                        and any(isinstance(child, ast.Name) and child.id in mutating_callbacks for child in ast.walk(item.test))
                        for item in helper.body
                        if isinstance(item, ast.Assert)
                    ):
                        continue
                    checks.extend(self.statements(helper.body, local, (*stack, name)))
                elif (imported := self.imported_helper(name)) is not None and name not in stack:
                    analysis, helper_name = imported
                    helper = analysis.helpers[helper_name]
                    local = dict(analysis.globals)
                    for arg, value in zip(helper.args.args, call.args, strict=False):
                        local[arg.arg] = self.value(value, env)
                        local["__fragment:" + arg.arg] = "yes" if self.fixed_text(value, env) else "no"
                    checks.extend(analysis.statements(helper.body, local, (*stack, name)))
                elif name.rsplit(".", 1)[-1].startswith("assert"):
                    if "Raises" in name or "Warns" in name or "assert_called" in name or "assert_not_called" in name:
                        checks.append("independent")
                    else:
                        kind = combine([self.value(argument, env) for argument in call.args])
                        checks.append("text-only" if kind == "text" else "constant-only" if kind == "constant" else "unknown")
                elif name.rsplit(".", 1)[-1].startswith(("check", "expect")):
                    checks.append("unknown")
        return checks


def analyze_python(
    source: str, path: pathlib.Path, root: pathlib.Path, loaded: dict[pathlib.Path, PythonAnalysis]
) -> tuple[list[TestFunction], dict[int, str]]:
    """テスト宣言ごとの比較とコメントの位置を返す。"""
    analysis = loaded.get(path)
    if analysis is None:
        analysis = PythonAnalysis(ast.parse(source), path, root, loaded)
    functions: list[TestFunction] = []

    def visit(body: list[ast.stmt], prefix: str = "") -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                env = dict(analysis.globals)
                env.update(
                    {argument.arg: "unknown" for argument in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
                )
                for decorator in node.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and _name(decorator.func).endswith("parametrize")
                        and len(decorator.args) >= 2
                    ):
                        names = decorator.args[0]
                        if isinstance(names, ast.Constant) and isinstance(names.value, str):
                            for name in names.value.split(","):
                                env[name.strip()] = analysis.value(decorator.args[1], env)
                                env["__fragment:" + name.strip()] = (
                                    "yes" if analysis.fixed_text(decorator.args[1], env) else "no"
                                )
                start = min([node.lineno, *[item.lineno for item in node.decorator_list]])
                functions.append(
                    TestFunction(
                        prefix + node.name,
                        node.lineno,
                        start,
                        node.end_lineno or node.lineno,
                        node.body[0].lineno,
                        analysis.statements(node.body, env),
                    )
                )

    visit(analysis.tree.body)
    comments = {
        token.start[0]: token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT
    }
    return functions, comments
