import argparse
import dataclasses
import importlib
import importlib.util
import json
import pathlib
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=pathlib.Path)
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    root = args.root.resolve()
    sys.path.insert(0, str(root))
    import pyfltr.command.slow_tests  # pylint: disable=import-outside-toplevel
    import pyfltr.parsing.entry  # pylint: disable=import-outside-toplevel
    import pyfltr.tools  # pylint: disable=import-outside-toplevel

    config_module = importlib.import_module(
        "pyfltr.parsing.pytest_config"
        if importlib.util.find_spec("pyfltr.parsing.pytest_config") is not None
        else "pyfltr.parsing.pytest"
    )
    detect_config_conflict = config_module.detect_pytest_config_conflict

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
        except Exception as exc:
            results[entry["id"]] = {"error": f"{type(exc).__name__}: {exc}"}
            continue
        parsed: dict[str, object] = {"diagnostics": [dataclasses.asdict(error) for error in errors]}
        info = pyfltr.tools.BUILTIN_COMMANDS.get(entry["tool"])
        if info is not None and getattr(info, "summary_parser", None) is not None:
            parsed["summary"] = pyfltr.parsing.entry.parse_summary(entry["tool"], output)
        if entry["tool"] == "pytest":
            parsed.update(
                config_conflict=detect_config_conflict(output),
                slow_tests=[dataclasses.asdict(item) for item in pyfltr.command.slow_tests.parse_pytest_durations(output)],
            )
        elif entry["tool"] == "vitest":
            slow_tests = pyfltr.command.slow_tests.parse_vitest_durations(output, base_cwd=pathlib.Path(entry["start_cwd"]))
            parsed["slow_tests"] = [dataclasses.asdict(item) for item in slow_tests]
        results[entry["id"]] = parsed

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
