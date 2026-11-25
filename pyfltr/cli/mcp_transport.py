import collections.abc
import contextlib
import functools
import inspect
import json
import subprocess
import sys
import typing

import anyio
import psutil
import pydantic

import pyfltr.cli.run_storage
import pyfltr.state.archive


def isolate_tool(
    handler: collections.abc.Callable[..., collections.abc.Awaitable[typing.Any]],
    raise_error: collections.abc.Callable[[str], typing.Never],
) -> collections.abc.Callable[..., collections.abc.Awaitable[typing.Any]]:
    signature = inspect.signature(handler, eval_str=True)
    adapter = pydantic.TypeAdapter(signature.return_annotation)
    name = typing.cast(typing.Any, handler).__name__

    @functools.wraps(handler)
    async def isolated(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        arguments = signature.bind(*args, **kwargs).arguments
        response = await _call_worker(name, dict(arguments))
        if "error" in response:
            raise_error(str(response["error"]))
        return adapter.validate_python(response["result"])

    isolated.__dict__["__signature__"] = signature
    return isolated


async def _call_worker(name: str, arguments: dict[str, typing.Any]) -> dict[str, typing.Any]:
    await anyio.lowlevel.checkpoint()
    run_id = pyfltr.state.archive.generate_run_id() if name == "tool_run" else None
    process: anyio.abc.Process | None = None
    with anyio.CancelScope(shield=True):
        process = await anyio.open_process(
            [sys.executable, "-I", "-m", "pyfltr.cli.mcp_worker", name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    assert process is not None
    cancelled = False
    try:
        assert process.stdin is not None and process.stdout is not None
        request = {"arguments": arguments, "run_id": run_id}
        await process.stdin.send(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        await process.stdin.aclose()
        parts: list[bytes] = []
        async for part in process.stdout:
            parts.append(part)
        returncode = await process.wait()
        if returncode != 0:
            return {
                "error": (
                    f"MCP処理の実行に失敗しました（終了コード {returncode}）。同じ引数で再実行し、"
                    "再発する場合はツール名・引数・この終了コードをMCPサーバーの管理者へ報告してください。"
                )
            }
        return json.loads(b"".join(parts))
    except anyio.get_cancelled_exc_class():
        cancelled = True
        raise
    finally:
        with anyio.CancelScope(shield=True):
            recovered = False
            try:
                if process.returncode is None:
                    recovered = await anyio.to_thread.run_sync(_stop_worker, process.pid)
            finally:
                await process.aclose()
            if cancelled and recovered and process.returncode is not None and run_id is not None:
                pyfltr.cli.run_storage.interrupt_archive(pyfltr.state.archive.ArchiveStore(), run_id, "mcp_cancelled")


def _stop_worker(pid: int) -> bool:
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return False
    targets = [parent, *reversed(children)]
    for target in targets:
        with contextlib.suppress(psutil.NoSuchProcess):
            target.terminate()
    _, alive = psutil.wait_procs(targets, timeout=5)
    for target in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            target.kill()
    _, remaining = psutil.wait_procs(alive, timeout=5)
    for target in remaining:
        with contextlib.suppress(psutil.NoSuchProcess):
            if target.status() != psutil.STATUS_ZOMBIE:
                return False
    return True
