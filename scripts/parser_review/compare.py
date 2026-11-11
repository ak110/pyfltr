"""同一の解析入力を基準版と作業ツリー版で解析し、診断全件と出力から取り出す値を比較する。

基準commitの`pyfltr/`全体を`git archive`で一時領域へ展開し、基準版と作業ツリー版を別のPythonプロセスで読み込む。
ファイルごとの`git show`で旧実装と現行の依存を混在させない。差分の改善・退行・同等の判定はレビュー担当が行う。

結果の区分は次のとおり。

- `equal`: 診断と追加の値（`extras`）が両版で一致し、診断の件数が採取時の件数と一致する
- `different`: 診断または追加の値が両版で異なる
- `count_mismatch`: 作業ツリー版の診断の件数が採取時にpyfltrが出した件数と異なる。
  原因は、比較の入力が実行時の解析入力と異なる場合と、作業ツリー版の解析が採取後に件数を変えた場合（退行を含む）がある。
  両版の一致を比べられた証拠にはできず、項目の`hint`が示す確認で原因を見分ける
- `error`: いずれかの版の解析が例外で終わった
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
        raise CompareError(
            f"基準commitを解決できません: {base}: {completed.stderr.strip()}。"
            "git logで存在するcommitを確認し、--baseへ指定してください"
        )
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
        raise CompareError(
            f"git archiveに失敗しました: {completed.stderr.decode(errors='backslashreplace').strip()}。"
            "対象リポジトリの読取権限と--baseのcommitを確認してcompareを再実行してください"
        )
    root = destination / "base"
    root.mkdir()
    with tarfile.open(archive_path) as tar:
        tar.extractall(root, filter="data")
    archive_path.unlink()


def _run_worker(root: pathlib.Path, inputs: pathlib.Path, output: pathlib.Path) -> dict[str, typing.Any]:
    entries = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped: dict[str, list[dict[str, typing.Any]]] = {}
    for entry in entries:
        grouped.setdefault(entry["start_cwd"], []).append(entry)
    merged: dict[str, typing.Any] = {"root": str(root), "modules": [], "outside_root": [], "results": {}}
    for index, (cwd, rows) in enumerate(grouped.items()):
        if not pathlib.Path(cwd).is_dir():
            raise CompareError(f"解析のstart_cwdが存在しません: {cwd}。採取時の作業ディレクトリを保持して再採取してください")
        group_input = output.with_name(f"{output.stem}-{index}.jsonl")
        group_output = output.with_name(f"{output.stem}-{index}.json")
        group_input.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        result = _run_worker_group(root, group_input, group_output, pathlib.Path(cwd))
        merged["results"].update(result["results"])
        merged["modules"].extend(result["modules"])
    merged["modules"] = sorted(set(merged["modules"]))
    output.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    return merged


def _run_worker_group(
    root: pathlib.Path, inputs: pathlib.Path, output: pathlib.Path, cwd: pathlib.Path
) -> dict[str, typing.Any]:
    completed = subprocess.run(
        [sys.executable, str(_WORKER), "--root", str(root), "--input", str(inputs), "--output", str(output)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
        cwd=cwd,
    )
    if not output.exists():
        raise CompareError(
            f"解析プロセスが結果を書きませんでした（root={root}）: {completed.stderr.strip()}。"
            "stderrの依存・入力エラーを修正してcompareを再実行してください"
        )
    result = json.loads(output.read_text(encoding="utf-8"))
    if completed.returncode != 0 or result.get("outside_root"):
        raise CompareError(
            f"解析プロセスが指定ルート外のpyfltrを読み込みました（root={root}）: {result.get('outside_root')}。"
            "PYTHONPATHと指定ルートのpyfltr配置を確認してcompareを再実行してください"
        )
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


_COUNT_MISMATCH_HINT = (
    "採取時の件数（collected）と作業ツリー版の件数（current）が異なる。"
    "基準版の件数（base）がcollectedと一致する場合は、作業ツリー版の解析が件数を変えた（退行を含む）ため、"
    "両版の診断を読んで改善・退行を判定する。"
    "baseもcollectedと異なる場合は、比較の入力（inputs.jsonlのoutput）が実行時の解析入力と異なるため、"
    "採取記録のparse_input・output.logとdiagnostics.jsonを読み、入力の取り方を直してから採取し直す。"
)


def _extras(base: dict[str, typing.Any], current: dict[str, typing.Any]) -> dict[str, dict[str, typing.Any]]:
    """診断以外に両版が出力から取り出した値（要約行・設定競合・遅い試験）を項目ごとに対にする。

    片方の版だけが持つ項目は、もう片方を`None`として差に含める。
    """
    keys = sorted((set(base) | set(current)) - {"diagnostics", "error"})
    return {key: {"base": base.get(key), "current": current.get(key)} for key in keys}


def compare(input_dir: pathlib.Path, output: pathlib.Path, *, base: str) -> dict[str, typing.Any]:
    """比較を実行して結果をJSONへ書き、集計を返す。"""
    inputs = input_dir / INPUTS_FILENAME
    if not inputs.is_file():
        raise CompareError(
            f"比較入力がありません: {inputs}。collectまたはpytest-formsを先に実行し、"
            "その出力ディレクトリを--inputへ指定してください"
        )
    entries = [json.loads(line) for line in inputs.read_text(encoding="utf-8").splitlines() if line.strip()]
    commit = _resolve_commit(base)
    with tempfile.TemporaryDirectory(prefix="parser-review-") as temp:
        temp_dir = pathlib.Path(temp)
        extract_base(commit, temp_dir)
        base_result = _run_worker(temp_dir / "base", inputs, temp_dir / "base.json")
        current_result = _run_worker(REPO_ROOT, inputs, temp_dir / "current.json")

    compared: list[dict[str, typing.Any]] = []
    counts = {"equal": 0, "different": 0, "count_mismatch": 0, "error": 0}
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
            item["extras"] = _extras(base_result["results"][entry["id"]], current_result["results"][entry["id"]])
            extra_equal = all(value["base"] == value["current"] for value in item["extras"].values())
            expected_count = entry.get("diagnostic_count")
            if expected_count is not None and expected_count != len(current_diagnostics):
                item["result"] = "count_mismatch"
                item["count_mismatch"] = {
                    "collected": expected_count,
                    "base": len(base_diagnostics),
                    "current": len(current_diagnostics),
                    "hint": _COUNT_MISMATCH_HINT,
                }
            else:
                item["result"] = "equal" if item["diff"]["equal"] and extra_equal else "different"
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
