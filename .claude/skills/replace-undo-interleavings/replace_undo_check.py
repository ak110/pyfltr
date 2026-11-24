import dataclasses
import inspect
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import typing

import pytilpack.modelcheck as mc

from pyfltr.grep_ import history, operations

EDIT_BYTES = b"EDIT foo\n"
INITIAL = {"a.txt": b"foo\r\nA\n", "b.txt": b"foo\nB\n"}
REPLACED = {name: data.replace(b"foo", b"bar") for name, data in INITIAL.items()}

_ORIG = {
    name: getattr(pathlib.Path, name)
    for name in ("read_bytes", "read_text", "write_bytes", "write_text", "exists", "unlink", "mkdir")
}
_WRITES = {"write_bytes", "write_text", "unlink", "mkdir"}
_local = threading.local()


@dataclasses.dataclass
class Fault:
    index: int
    partial: bool


class Harness:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.work = root / "work"
        self.cache = root / "cache"
        self.files = [self.work / name for name in INITIAL]
        self.fault: Fault | None = None
        self.reset()

    def reset(self) -> None:
        self.counter = 0
        self.fault_hit = False
        self.save_failed = False
        self.failed_read: pathlib.Path | None = None
        self.failed_step: int | None = None
        self.last_comparison: pathlib.Path | None = None
        self.interrupted_file: pathlib.Path | None = None

    def classify(self, path: pathlib.Path) -> tuple[str, str] | None:
        if not path.is_absolute():
            path = self.work / path
        if path in self.files:
            return str(path), path.name
        if path == self.work:
            return "dir:work", "work/"
        try:
            rel = path.relative_to(self.cache / "replaces")
        except ValueError:
            return None
        parts = rel.parts[1:]
        return "history", "replaces/" + "/".join(parts)

    def wrap(self, name: str) -> typing.Callable[..., typing.Any]:
        orig = _ORIG[name]
        harness = self

        def patched(path: pathlib.Path, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
            if not getattr(_local, "active", False):
                return orig(path, *args, **kwargs)
            key = harness.classify(path)
            if key is None:
                return orig(path, *args, **kwargs)
            resource, display = key
            operation = name
            if name == "read_bytes":
                frame = inspect.currentframe()
                if frame is not None and frame.f_back is not None and frame.f_back.f_code.co_name == "_read_current":
                    operation = "compare"
                del frame
            mc.step(resource, write=name in _WRITES, label=f"{operation} {display}")
            index = harness.counter
            harness.counter += 1
            fault = harness.fault
            if fault is not None and fault.index == index:
                harness.fault_hit = True
                harness.failed_step = index
                harness.save_failed = resource == "history" and name in {"mkdir", "write_text", "write_bytes"}
                file = pathlib.Path(resource)
                if operation == "read_bytes" and file in harness.files:
                    harness.failed_read = file
                if name == "write_bytes" and file in harness.files:
                    harness.interrupted_file = file
                elif name == "mkdir" and resource == "dir:work":
                    harness.interrupted_file = harness.last_comparison
                if fault.partial and name == "write_bytes" and resource != "history" and not resource.startswith("dir:"):
                    data = args[0]
                    orig(path, data[: len(data) // 2])
                raise OSError(f"注入した失敗: {name} {display}")
            result = orig(path, *args, **kwargs)
            if operation == "compare":
                harness.last_comparison = pathlib.Path(resource)
            return result

        return patched

    def install(self) -> None:
        for name in _ORIG:
            setattr(pathlib.Path, name, self.wrap(name))

    def uninstall(self) -> None:
        for name, orig in _ORIG.items():
            setattr(pathlib.Path, name, orig)


def actor(func: typing.Callable[[], typing.Any]) -> typing.Callable[[], typing.Any]:
    def run() -> typing.Any:
        _local.active = True
        try:
            return func()
        finally:
            _local.active = False

    return run


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="replace-interleavings-")).resolve()
    try:
        return check(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_request(files: list[pathlib.Path]) -> operations.ReplaceRequest:
    return operations.ReplaceRequest(
        targets=operations.TargetRequest(list(files), no_exclude=True, no_gitignore=True),
        pattern="foo",
        replacement="bar",
    )


def check(tmp: pathlib.Path) -> int:
    root = tmp / "run"
    template = tmp / "template"
    os.environ["PYFLTR_CACHE_DIR"] = str(root / "cache")
    os.environ["PYFLTR_GLOBAL_CONFIG"] = str(tmp / "global.toml")
    harness = Harness(root)
    harness.work.mkdir(parents=True)
    os.chdir(harness.work)
    for file in harness.files:
        file.write_bytes(INITIAL[file.name])
    result = operations.execute_replace(make_request(harness.files))
    assert result.replace_id is not None
    replace_id = result.replace_id
    shutil.copytree(root, template / "undo")

    def setup_replace() -> None:
        harness.reset()
        shutil.rmtree(harness.cache, ignore_errors=True)
        for file in harness.files:
            file.write_bytes(INITIAL[file.name])

    def setup_undo() -> None:
        harness.reset()
        shutil.rmtree(harness.cache, ignore_errors=True)
        shutil.copytree(template / "undo" / "cache", harness.cache)
        for file in harness.files:
            file.write_bytes(REPLACED[file.name])

    def do_replace() -> typing.Any:
        try:
            res = operations.execute_replace(make_request(harness.files))
        except history.ReplaceFailure as exc:
            return ("failure", exc.describe(undo="<undo>"), [])
        return ("ok", "", [file for file, _ in res.prepared])

    def do_undo(force: bool) -> typing.Callable[[], typing.Any]:
        def run() -> typing.Any:
            store = history.ReplaceHistoryStore()
            try:
                restored, skipped, _ = store.undo_replace(replace_id, force=force)
            except history.ReplaceFailure as exc:
                return ("failure", exc.describe(undo="<undo>"), [])
            if skipped:
                return ("skipped", "", skipped)
            return ("ok", "", restored)

        return run

    scenarios = [
        ("replace", setup_replace, do_replace, INITIAL, REPLACED, False),
        ("undo", setup_undo, do_undo(False), REPLACED, INITIAL, False),
        ("undo-force", setup_undo, do_undo(True), REPLACED, INITIAL, True),
    ]
    harness.install()
    violations: list[str] = []
    total_runs = 0
    excluded = 0
    started = time.perf_counter()
    try:
        for name, setup, operation, start, target, force in scenarios:
            edits: list[tuple[pathlib.Path | None, bytes]] = [(None, EDIT_BYTES)]
            for file in harness.files:
                data = target[file.name]
                edits.extend(((file, EDIT_BYTES), (file, data[: len(data) // 2])))
            for edited, edit_bytes in edits:
                for partial in (False, True):
                    index = -1 if not partial else 0
                    while True:
                        harness.fault = Fault(index, partial) if index >= 0 else None
                        hit_any = False
                        actors: dict[str, typing.Callable[[], typing.Any]] = {"pyfltr": actor(operation)}
                        if edited is not None:
                            target_file = edited

                            def edit(target_file: pathlib.Path = target_file, data: bytes = edit_bytes) -> None:
                                mc.step(str(target_file), write=True, label=f"edit {target_file.name}")
                                _ORIG["write_bytes"](target_file, data)

                            actors["editor"] = actor(edit)
                        explorer = mc.ThreadExplorer(actors, setup)
                        for run in explorer:
                            total_runs += 1
                            hit_any = hit_any or harness.fault_hit
                            problem, skipped = judge(run, harness, edited, edit_bytes, start, target, force)
                            excluded += skipped
                            if problem:
                                steps = [f"{step.actor}:{step.label}" for step in run.steps]
                                violations.append(
                                    f"{name} edit={edited and edited.name}:{edit_bytes!r} fault={harness.fault}: {problem}"
                                    f"\n  steps={steps}"
                                )
                        assert explorer.outcome is mc.Outcome.EXHAUSTIVE
                        if index >= 0 and not hit_any:
                            break
                        index += 1
                        if index > 200:
                            raise AssertionError("fault index runaway")
    finally:
        harness.uninstall()
    elapsed = time.perf_counter() - started
    print(f"runs={total_runs} excluded={excluded} violations={len(violations)} elapsed={elapsed:.1f}s")
    seen = set()
    for v in violations:
        head = v.split("\n")[0]
        if head in seen:
            continue
        seen.add(head)
        print(v)
    return 1 if violations else 0


def judge(
    run: mc.ThreadRun,
    harness: Harness,
    edited: pathlib.Path | None,
    edit_bytes: bytes,
    start: dict[str, bytes],
    target: dict[str, bytes],
    force: bool,
) -> tuple[str | None, int]:
    if "pyfltr" in run.exceptions:
        exc = run.exceptions["pyfltr"]
        if not isinstance(exc, OSError) or not str(exc).startswith("注入した失敗:"):
            return f"予期しない例外: {exc!r}", 0
        outcome, message, done = "error", str(exc), []
    else:
        outcome, message, done = run.returns["pyfltr"]
    if "editor" in run.exceptions:
        return f"別の編集に失敗: {run.exceptions['editor']!r}", 0
    if harness.save_failed and target is REPLACED:
        for file in harness.files:
            expected = edit_bytes if file == edited else start[file.name]
            if file.read_bytes() != expected:
                return f"履歴保存の失敗で対象が変わった: {file.name}", 0
        if outcome != "failure":
            return f"履歴保存に失敗したのにエラーを返さなかった: {outcome}", 0
        return None, 0

    unresolved = reported_paths(message, "戻せなかったファイルがあります: ")
    conflicts = reported_paths(message, "上書きせずに残したファイルがあります: ")
    reported = unresolved | conflicts
    finals = {file: (_ORIG["read_bytes"](file) if _ORIG["exists"](file) else None) for file in harness.files}
    starts = dict(start)
    targets = dict(target)
    concurrent = False
    excluded = 0
    conflict_observed = False
    if edited is not None:
        steps = list(run.steps)
        e = next(i for i, s in enumerate(steps) if s.actor == "editor")
        own = [(i, (s.label or "").split(" ")[0]) for i, s in enumerate(steps) if s.actor == "pyfltr"]
        accesses = [(i, op, rank) for rank, (i, op) in enumerate(own) if steps[i].resource == str(edited)]
        before = [(op, rank) for i, op, rank in accesses if i < e]
        after = [(op, rank) for i, op, rank in accesses if i > e]
        if not any(op in ("read_bytes", "read_text", "compare") and rank != harness.failed_step for op, rank in before):
            starts[edited.name] = edit_bytes
            targets[edited.name] = edit_bytes.replace(b"foo", b"bar") if target is REPLACED else target[edited.name]
            if not force and target is INITIAL:
                targets[edited.name] = edit_bytes
        else:
            concurrent = True
            last_op, rank = before[-1]
            if last_op == "compare" and rank != harness.failed_step and after and after[0][0] in ("write_bytes", "unlink"):
                excluded = 1
            final = finals[edited]
            if not excluded and final != edit_bytes:
                return f"別の編集が消えた: {edited.name} -> {final!r} outcome={outcome} {message}", 0
            compared = any(op == "compare" and rank != harness.failed_step for op, rank in after)
            if not excluded and compared:
                if edited == harness.interrupted_file:
                    if edited.name not in unresolved:
                        return f"途中で失敗した書込みの未復元を報告していない: {edited.name} {message}", 0
                else:
                    conflict_observed = True
                    if outcome != "failure" or edited.name not in conflicts:
                        return f"別の編集を競合として報告していない: {edited.name} {message}", 0

    must_skip = target is INITIAL and not force and starts != start
    if harness.failed_step is None:
        expected_outcome = "skipped" if must_skip else "failure" if conflict_observed else "ok"
        if outcome != expected_outcome:
            return f"障害のない実行で期待した結果と異なる: {expected_outcome=} {outcome=} {message}", 0
    if outcome == "ok":
        if must_skip:
            return "変更済みのファイルがあるのに通常のundoが成功した", 0
        expected_done = set(harness.files)
        if target is REPLACED:
            expected_done = {file for file in harness.files if file != harness.failed_read and b"foo" in starts[file.name]}
        if {harness.work / path for path in done} != expected_done:
            return f"正常終了なのに処理済みのファイルが不足または過剰: {done!r}", 0
    else:
        if outcome == "skipped":
            if not must_skip:
                return "変更済みの対象がないのに全件をスキップした", 0
            if {harness.work / path for path in done} != set(harness.files):
                return f"スキップした対象の報告が不足または過剰: {done!r}", 0
        expected_done = set()

    for file in harness.files:
        if concurrent and file == edited:
            continue
        final = finals[file]
        is_start = final == starts[file.name]
        is_target = final == targets[file.name]
        if outcome == "ok":
            expect_target = file in expected_done
            if expect_target and not is_target:
                return f"正常終了なのに目標の内容でない: {file.name} -> {final!r}", 0
            if not expect_target and not is_start:
                return f"対象外のファイルが変わった: {file.name} -> {final!r}", 0
        elif outcome == "skipped":
            if not is_start:
                return f"スキップなのに変わった: {file.name} -> {final!r}", 0
        else:
            if not is_start and file.name not in reported:
                return f"失敗後に開始前でなく報告もない: {file.name} -> {final!r} {message}", 0
    return None, excluded


def reported_paths(message: str, marker: str) -> set[str]:
    report = message.partition("原因:")[0]
    _, found, suffix = report.partition(marker)
    if not found:
        return set()
    listed = suffix.partition("。")[0]
    return {pathlib.PurePosixPath(path).name for path in listed.split(", ")}


if __name__ == "__main__":
    sys.exit(main())
