import asyncio
import contextlib
import inspect
import json
import logging
import sys

import mcp.server.mcpserver.exceptions
import pydantic

import pyfltr.cli.mcp_server


def main() -> int:
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(levelname)s: %(message)s")
    if len(sys.argv) != 2 or sys.argv[1] not in pyfltr.cli.mcp_server.TOOL_HANDLERS:
        sys.stderr.write("MCP workerの処理名が不正です。サーバーから起動してください。\n")
        return 1
    handler = pyfltr.cli.mcp_server.TOOL_HANDLERS[sys.argv[1]]
    # IPCは親がUTF-8のバイト列で送る。Windowsの標準入出力の既定文字コードへ委ねない。
    arguments = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = asyncio.run(handler(**arguments))
        adapter = pydantic.TypeAdapter(inspect.signature(handler, eval_str=True).return_annotation)
        response = {"result": adapter.dump_python(result, mode="json")}
    except mcp.server.mcpserver.exceptions.ToolError as exc:
        response = {"error": str(exc)}
    except Exception:
        logging.exception("MCP workerの処理に失敗しました")
        return 1
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
