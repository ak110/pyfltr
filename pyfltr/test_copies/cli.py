"""写し候補の検査と診断の出力。"""

import argparse
import io
import json
import pathlib
import sys
import tokenize

from pyfltr.test_copies.foreign import analyze_foreign
from pyfltr.test_copies.model import TestFunction
from pyfltr.test_copies.python import PythonAnalysis, analyze_python

_MARKER = "test-copy-contract:"


def _next_action(kind: object, path: pathlib.Path, decode_failed: bool) -> str:
    if kind == "input-error":
        if decode_failed:
            return "UTF-8で保存し直すか、UTF-8で読めるファイルを指定する。"
        return "対象の構文・パス・読取権限を修正する。"
    marker = "#" if _language(path) == "python" else "//"
    contract = f"{marker} {_MARKER} <保証する独立契約の理由>"
    if kind == "invalid-exception":
        return f"重複や空の例外を除き、正当な契約には関数の直前または本文先頭に {contract} を1つ置く。"
    return (
        "写しだけの関数を削除するか、独立した結果の検査へ置き換える。"
        f"正当な契約には {contract} を関数の直前または本文先頭に置く。"
    )


def _exceptions(functions: list[TestFunction], comments: dict[int, str]) -> list[tuple[int, str]]:
    errors = []
    used: set[int] = set()
    for function in functions:
        locations = [function.start - 1, *range(function.start, min(function.body_start, function.end) + 1)]
        markers = [(line, comments[line]) for line in locations if line in comments and _MARKER in comments[line]]
        for line, comment in markers:
            if line in used:
                errors.append((line, "1つの例外を複数関数へ適用できない"))
            used.add(line)
            reason = comment.split(_MARKER, 1)[1].strip()
            if not reason or function.reason is not None:
                errors.append((line, "例外理由が空または重複している"))
            else:
                function.reason = reason
    for line, comment in comments.items():
        if _MARKER in comment and line not in used:
            errors.append((line, "例外は1関数の直前または本文先頭に置く"))
    return errors


def _language(path: pathlib.Path) -> str | None:
    if path.name.endswith(("_test.py", "_spec.py")):
        return "python"
    if path.name.endswith((".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")):
        return "typescript"
    if path.suffix == ".rs":
        return "rust"
    return None


def main(argv: list[str] | None = None) -> int:
    """明示されたテストファイルを静的に検査する。"""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=pathlib.Path, help="root相対または絶対パス。未追跡ファイルも可")
    parser.add_argument("--root", type=pathlib.Path, default=pathlib.Path.cwd(), help="相対パスとPython補助関数の解決起点")
    parser.add_argument("--json", action="store_true", help="対象全パス・関数・例外・診断をJSONで出力")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    files: list[dict[str, object]] = []
    diagnostics: list[dict[str, object]] = []
    decode_failures: set[str] = set()
    loaded: dict[pathlib.Path, PythonAnalysis] = {}
    try:
        if not root.is_dir():
            raise ValueError(f"rootがディレクトリではない: {root}")
        paths = sorted({root / path for path in args.paths})
        for path in paths:
            display = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()
            language = _language(path)
            entry: dict[str, object] = {"path": display, "language": language, "status": "out_of_scope", "functions": []}
            files.append(entry)
            try:
                if language is None:
                    continue
                source = path.read_text(encoding="utf-8")
                functions, comments = (
                    analyze_python(source, path, root, loaded)
                    if language == "python"
                    else analyze_foreign(source, language == "rust")
                )
                entry["status"] = "analyzed" if functions else "out_of_scope"
                for line, message in _exceptions(functions, comments):
                    diagnostics.append({"path": display, "line": line, "kind": "invalid-exception", "message": message})
                results = []
                for function in functions:
                    kinds = sorted(set(function.checks) & {"text-only", "constant-only"})
                    candidate = bool(kinds) and all(
                        check in {"text-only", "constant-only", "startup", "text-support"} for check in function.checks
                    )
                    status = (
                        "exempt"
                        if function.reason
                        else "candidate"
                        if candidate
                        else "unclassified"
                        if "unknown" in function.checks or not function.checks
                        else "clear"
                    )
                    results.append(
                        {
                            "id": function.id,
                            "line": function.line,
                            "status": status,
                            "kinds": kinds,
                            "reason": function.reason,
                            "checks": function.checks,
                        }
                    )
                    if status == "candidate":
                        diagnostics.append(
                            {
                                "path": display,
                                "line": function.line,
                                "kind": "+".join(kinds),
                                "message": f"{function.id}: 文言・定数の比較だけの写し候補",
                            }
                        )
                entry["functions"] = results
            except (OSError, UnicodeError, SyntaxError, ValueError, tokenize.TokenError, RecursionError) as error:
                entry["status"] = "error"
                if isinstance(error, UnicodeDecodeError):
                    decode_failures.add(display)
                diagnostics.append(
                    {
                        "path": display,
                        "line": getattr(error, "lineno", None) or 1,
                        "kind": "input-error",
                        "message": " ".join(str(error).splitlines()),
                    }
                )
    except (OSError, ValueError, RuntimeError) as error:
        diagnostics.append({"path": str(root), "line": 1, "kind": "input-error", "message": " ".join(str(error).splitlines())})
    actions = [
        _next_action(item["kind"], pathlib.Path(display), display in decode_failures)
        for item in diagnostics
        for display in (str(item["path"]),)
    ]
    if args.json:
        response = {"schema_version": 1, "root": str(root), "files": files, "diagnostics": diagnostics}
        if diagnostics:
            response["next_action"] = "次の操作: " + " ".join(dict.fromkeys(actions))
        print(json.dumps(response, ensure_ascii=False, indent=2))
    else:
        for diagnostic, action in zip(diagnostics, actions, strict=True):
            print(
                f"{diagnostic['path']}:{diagnostic['line']}: error: {diagnostic['kind']}: "
                f"{diagnostic['message']} 次の操作: {action}",
                file=sys.stderr,
            )
    if any(item["kind"] in {"input-error", "invalid-exception"} for item in diagnostics):
        return 2
    return 1 if diagnostics else 0
