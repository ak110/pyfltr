"""同一の生出力を基準版と作業ツリー版の`parse_errors`で解析し、診断全件を比較する。

基準commitの`pyfltr/`全体を`git archive`で一時領域へ展開し、基準版と作業ツリー版を別のPythonプロセスで読み込む。
ファイルごとの`git show`で旧実装と現行の依存を混在させない。差分の改善・退行・同等の判定はレビュー担当が行う。
"""

import collections
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import typing

from scripts.parser_review.collect import INPUTS_FILENAME, REPO_ROOT

_WORKER = pathlib.Path(__file__).resolve().with_name("parse_worker.py")


class CompareError(Exception):
    """比較を成立させられない入力・環境の誤り。"""


def _resolve_commit(base: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise CompareError(f"基準commitを解決できません: {base}: {completed.stderr.strip()}")
    return completed.stdout.strip()


def extract_base(commit: str, destination: pathlib.Path) -> None:
    """基準commitの`pyfltr/`全体を展開する。"""
    archive_path = destination / "base.tar"
    with archive_path.open("wb") as f:
        completed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "--format=tar", commit, "--", "pyfltr"],
            stdout=f,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode != 0:
        raise CompareError(f"git archiveに失敗しました: {completed.stderr.decode(errors='backslashreplace').strip()}")
    root = destination / "base"
    root.mkdir()
    with tarfile.open(archive_path) as tar:
        tar.extractall(root, filter="data")
    archive_path.unlink()


def _run_worker(root: pathlib.Path, inputs: pathlib.Path, output: pathlib.Path) -> dict[str, typing.Any]:
    completed = subprocess.run(
        [sys.executable, str(_WORKER), "--root", str(root), "--input", str(inputs), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if not output.exists():
        raise CompareError(f"解析プロセスが結果を書きませんでした（root={root}）: {completed.stderr.strip()}")
    result = json.loads(output.read_text(encoding="utf-8"))
    if completed.returncode != 0 or result.get("outside_root"):
        raise CompareError(f"解析プロセスが指定ルート外のpyfltrを読み込みました（root={root}）: {result.get('outside_root')}")
    return result


def _canonical(diagnostic: dict[str, typing.Any]) -> str:
    return json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)


def diff_diagnostics(base: list[dict[str, typing.Any]], current: list[dict[str, typing.Any]]) -> dict[str, typing.Any]:
    """`ErrorLocation`の全フィールドで両版の診断を比べる。

    `only_base`・`only_current`は重複を数えた多重集合の差、`order_changed`は集合が同じで順序だけが異なることを示す。
    """
    base_counter = collections.Counter(_canonical(item) for item in base)
    current_counter = collections.Counter(_canonical(item) for item in current)
    only_base = [json.loads(item) for item in (base_counter - current_counter).elements()]
    only_current = [json.loads(item) for item in (current_counter - base_counter).elements()]
    return {
        "equal": base == current,
        "order_changed": base != current and not only_base and not only_current,
        "only_base": only_base,
        "only_current": only_current,
    }


def _side(result: dict[str, typing.Any], entry_id: str) -> tuple[list[dict[str, typing.Any]] | None, str | None]:
    value = result["results"].get(entry_id)
    if value is None:
        return None, "結果が無い"
    if "error" in value:
        return None, value["error"]
    return value["diagnostics"], None


def compare(input_dir: pathlib.Path, output: pathlib.Path, *, base: str) -> dict[str, typing.Any]:
    """比較を実行して結果をJSONへ書き、集計を返す。"""
    inputs = input_dir / INPUTS_FILENAME
    if not inputs.is_file():
        raise CompareError(f"比較入力がありません: {inputs}")
    entries = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines() if line.strip()]
    commit = _resolve_commit(base)
    with tempfile.TemporaryDirectory(prefix="parser-review-") as temp:
        temp_dir = pathlib.Path(temp)
        extract_base(commit, temp_dir)
        base_result = _run_worker(temp_dir / "base", inputs, temp_dir / "base.json")
        current_result = _run_worker(REPO_ROOT, inputs, temp_dir / "current.json")

    compared: list[dict[str, typing.Any]] = []
    counts = {"equal": 0, "different": 0, "error": 0}
    for entry in entries:
        base_diagnostics, base_error = _side(base_result, entry["id"])
        current_diagnostics, current_error = _side(current_result, entry["id"])
        item: dict[str, typing.Any] = {
            "id": entry["id"],
            "tool": entry["tool"],
            "output": entry["output"],
            "derived_from": entry.get("derived_from"),
            "transform": entry.get("transform"),
            "base": base_diagnostics if base_error is None else {"error": base_error},
            "current": current_diagnostics if current_error is None else {"error": current_error},
        }
        if base_diagnostics is None or current_diagnostics is None:
            item["result"] = "error"
            counts["error"] += 1
        else:
            item["diff"] = diff_diagnostics(base_diagnostics, current_diagnostics)
            item["result"] = "equal" if item["diff"]["equal"] else "different"
            counts[item["result"]] += 1
        compared.append(item)
    report = {
        "base": {"revision": base, "commit": commit, "pyfltr_root": base_result["root"]},
        "current": {"pyfltr_root": current_result["root"]},
        "input": str(inputs),
        "counts": counts,
        "total": len(compared),
        "entries": compared,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
