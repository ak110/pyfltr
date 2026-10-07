"""実装と単体テストの配置、および横断テストの置き場所を検証する。"""

import pathlib


def placement_violations(repository: pathlib.Path) -> list[str]:
    """単体テストと同じ相対位置に対応する実装があるか調べる。"""
    violations: list[str] = []
    for test in sorted((repository / "tests").rglob("*_test.py")):
        relative = test.relative_to(repository / "tests")
        if relative.parts[0] == "integration" or "smoke_data" in relative.parts:
            continue
        module = relative.with_name(relative.name.removesuffix("_test.py"))
        candidates = [
            repository / "pyfltr" / module.with_suffix(".py"),
            repository / "pyfltr" / module.with_name(module.name + "_").with_suffix(".py"),
        ]
        if not any(candidate.is_file() for candidate in candidates):
            violations.append(str(relative))
    return violations


def test_unit_tests_follow_implementation_modules() -> None:
    """横断テスト以外は実装モジュールと同じ位置へ置く。"""
    repository = pathlib.Path(__file__).resolve().parents[2]
    assert not placement_violations(repository)
