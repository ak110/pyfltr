"""指定したルートの`pyfltr`だけを読み込み、比較入力を`parse_errors`で解析する子プロセス。

`compare`が基準版（`git archive`で展開した一時領域）と作業ツリー版のそれぞれについて別プロセスで起動する。
ファイルとして起動するため、`sys.path`の先頭は本ファイルのディレクトリになる。指定ルートを先頭へ加えてから
`pyfltr`を読み込み、読み込んだ全モジュールが指定ルート配下にあることを確かめる。
"""

import argparse
import dataclasses
import json
import pathlib
import sys


def main() -> int:
    """入力JSON Linesを解析し、診断全件をJSONへ書く。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=pathlib.Path)
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    import pyfltr.parsing.entry  # pylint: disable=import-outside-toplevel

    results: dict[str, object] = {}
    for line in args.input.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        output = pathlib.Path(entry["output"]).read_text(encoding="utf-8")
        try:
            errors = pyfltr.parsing.entry.parse_errors(
                entry["tool"],
                output,
                path_base=pathlib.Path(entry["path_base"]),
                start_cwd=pathlib.Path(entry["start_cwd"]),
            )
        except Exception as exc:  # 版ごとの例外も比較結果として残す
            results[entry["id"]] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        results[entry["id"]] = {"diagnostics": [dataclasses.asdict(error) for error in errors]}

    module_files = sorted(
        {
            str(pathlib.Path(module_file).resolve())
            for name, module in sys.modules.items()
            if (name == "pyfltr" or name.startswith("pyfltr.")) and (module_file := getattr(module, "__file__", None))
        }
    )
    outside = [path for path in module_files if not pathlib.Path(path).is_relative_to(root)]
    args.output.write_text(
        json.dumps(
            {"root": str(root), "modules": module_files, "outside_root": outside, "results": results}, ensure_ascii=False
        ),
        encoding="utf-8",
    )
    return 1 if outside else 0


if __name__ == "__main__":
    sys.exit(main())
