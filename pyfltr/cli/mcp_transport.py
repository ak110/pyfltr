"""MCP受信ループと、同期処理・共有状態・子プロセス所有権の分離。"""

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


def isolate_tool(
    handler: collections.abc.Callable[..., collections.abc.Awaitable[typing.Any]],
    raise_error: collections.abc.Callable[[str], typing.Never],
) -> collections.abc.Callable[..., collections.abc.Awaitable[typing.Any]]:
    """公開スキーマを維持し、要求ごとのworkerへ同期処理を委ねる。

    スレッドだけの分離ではwarning収集とloggerの出力先が共有され、
    mise/gitの直接起動も実行固有の停止登録簿に入らない。プロセス境界で両方を分離する。
    """
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

    # SDKがwrapperのglobalsを使って評価しても、公開モデルの型が解決できるようにする。
    isolated.__dict__["__signature__"] = signature
    return isolated


async def _call_worker(name: str, arguments: dict[str, typing.Any]) -> dict[str, typing.Any]:
    await anyio.lowlevel.checkpoint()
    process: anyio.abc.Process | None = None
    # 起動後・handle取得前のキャンセルで所有者を失わないよう、この区間だけ保護する。
    with anyio.CancelScope(shield=True):
        process = await anyio.open_process(
            [sys.executable, "-I", "-m", "pyfltr.cli.mcp_worker", name],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    assert process is not None
    try:
        assert process.stdin is not None and process.stdout is not None
        await process.stdin.send(json.dumps(arguments, ensure_ascii=False).encode("utf-8"))
        await process.stdin.aclose()
        parts: list[bytes] = []
        async for part in process.stdout:
            parts.append(part)
        returncode = await process.wait()
        if returncode != 0:
            return {"error": f"MCP処理の実行に失敗しました（終了コード {returncode}）。サーバーのstderrを確認してください。"}
        return json.loads(b"".join(parts))
    finally:
        # SDKのlevel cancellation中も子孫停止とworker回収を完了してから取り消しを返す。
        with anyio.CancelScope(shield=True):
            if process.returncode is None:
                await anyio.to_thread.run_sync(_stop_worker, process.pid)
            await process.aclose()


def _stop_worker(pid: int) -> None:
    """起動handleが返したworkerと、その時点で所有する子孫だけを停止する。

    ツールは独自のprocess groupを作成するため、workerのgroup停止だけでは届かない。
    PIDから取得したpsutil.Processは生成時刻も照合し、別要求のプロセスを対象にしない。
    """
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
    except psutil.NoSuchProcess:
        return
    targets = [parent, *reversed(children)]
    for target in targets:
        with contextlib.suppress(psutil.NoSuchProcess):
            target.terminate()
    _, alive = psutil.wait_procs(targets, timeout=5)
    for target in alive:
        with contextlib.suppress(psutil.NoSuchProcess):
            target.kill()
    psutil.wait_procs(alive, timeout=5)
