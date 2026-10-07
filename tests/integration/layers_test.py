import ast
import pathlib

LAYERS = {
    "config": 1,
    "command": 2,
    "output": 3,
    "state": 4,
    "grep_": 5,
    "cli": 6,
    "__main__": 6,
}


def layer(module: str) -> int:
    """pyfltr内のモジュールに対応する層番号を返す。"""
    return LAYERS.get(module.split(".")[1], 0)


def import_violations(package: pathlib.Path) -> list[str]:
    """全ASTノードのpyfltr内importを解決し、逆方向の参照を列挙する。"""
    violations: list[str] = []
    for path in sorted(package.rglob("*.py")):
        parts = path.relative_to(package.parent).with_suffix("").parts
        module = ".".join(parts)
        parent = parts if path.name == "__init__.py" else parts[:-1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = list(parent[: len(parent) - node.level + 1]) if node.level else []
                if node.module:
                    base.extend(node.module.split("."))
                prefix = ".".join(base)
                targets = [prefix] if len(base) > 1 else [f"{prefix}.{alias.name}" for alias in node.names]
            else:
                continue
            for target in targets:
                if target.startswith("pyfltr.") and layer(target) > layer(module):
                    violations.append(f"{module}:{node.lineno} -> {target}")
    return violations


def test_imports_follow_layers() -> None:
    """CLIから下位への方向で全てのimportが成立する。"""
    package = pathlib.Path(__file__).resolve().parents[2] / "pyfltr"
    violations = import_violations(package)
    assert not violations, "\n".join(violations)
